"""Spec: the per-tool wall-clock budget is disclosed to the agent, but stated
ONCE (in the system-prompt affordance) rather than repeated on every tool. A
tool's DESCRIPTION carries a note ONLY when its budget DIFFERS from the default.

Two layers covered here:
  - install_tool_timeout: default tools get NO note; exceptions (timeout_s
    override / None opt-out) do. Introspected via the public FastMCP path
    (await mcp.list_tools() -> Tool.description), the same one
    test_tool_timeout.py::test_real_fastmcp_schema_unchanged uses for inputSchema.
  - tool_timeout_affordance + runtime.options: the default is stated once and
    wired into the system prompt only when the browser-harness server is loaded.

asyncio_mode=auto (pyproject) → async tests need no decorator.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_server.tool_timeout import (
    _BUDGET_MARKER,
    install_tool_timeout,
    tool_timeout_affordance,
)


async def _describe(mcp: FastMCP, name: str) -> str:
    tools = await mcp.list_tools()
    return next(t for t in tools if t.name == name).description or ""


# ── per-tool notes: default = silent, exceptions = noted ─────────────────────


async def test_default_tool_gets_no_note():
    # The common case: a bare tool inherits the default, so it carries NO note —
    # the default is stated once in the system prompt, not repeated here.
    mcp = FastMCP("t")
    install_tool_timeout(mcp, default_s=300.0)

    @mcp.tool()
    async def probe(url: str) -> dict:
        """Probe a URL."""
        return {"ok": True}

    desc = await _describe(mcp, "probe")
    assert desc.strip() == "Probe a URL."  # docstring only, nothing appended
    assert _BUDGET_MARKER not in desc
    assert "300" not in desc


async def test_explicit_value_equal_to_default_gets_no_note():
    # Explicit but == default ⇒ still no note (matches the global statement).
    mcp = FastMCP("t")
    install_tool_timeout(mcp, default_s=300.0)

    @mcp.tool(timeout_s=300)
    async def same() -> dict:
        """Same as default."""
        return {"ok": True}

    desc = await _describe(mcp, "same")
    assert _BUDGET_MARKER not in desc


async def test_opt_out_none_says_no_timeout():
    mcp = FastMCP("t")
    install_tool_timeout(mcp, default_s=300.0)

    @mcp.tool(timeout_s=None)
    async def blocking() -> dict:
        """Blocks on a human."""
        return {"ok": True}

    desc = await _describe(mcp, "blocking")
    assert "Blocks on a human." in desc
    assert _BUDGET_MARKER in desc
    # opted-out tools advertise no bound, and must NOT claim a number
    assert ("no wall-clock timeout" in desc.lower()) or ("indefinitely" in desc.lower())
    assert "300" not in desc


async def test_explicit_660_override_is_noted():
    mcp = FastMCP("t")
    install_tool_timeout(mcp, default_s=300.0)

    @mcp.tool(timeout_s=660)
    async def takeover() -> dict:
        """Human takeover."""
        return {"ok": True}

    desc = await _describe(mcp, "takeover")
    assert "Human takeover." in desc  # docstring kept
    assert _BUDGET_MARKER in desc
    assert "660" in desc  # the real browser_request_user_login bound
    assert "300" not in desc  # the default never appears on the tool


async def test_description_kwarg_exception_gets_note_on_kwarg():
    # When a tool sets both description= (which FastMCP prefers over the docstring)
    # and a non-default timeout, the note must land on the kwarg.
    mcp = FastMCP("t")
    install_tool_timeout(mcp, default_s=300.0)

    @mcp.tool(description="Explicit desc.", timeout_s=660)
    async def kwarged() -> dict:
        """Docstring is ignored when description= is set."""
        return {"ok": True}

    desc = await _describe(mcp, "kwarged")
    assert "Explicit desc." in desc
    assert "Docstring is ignored" not in desc  # description= wins
    assert "660" in desc
    assert _BUDGET_MARKER in desc


async def test_note_not_double_appended():
    # Idempotency guard: the marker (and so the note) appears exactly once.
    mcp = FastMCP("t")
    install_tool_timeout(mcp, default_s=300.0)

    @mcp.tool(timeout_s=660)
    async def once() -> dict:
        """Once."""
        return {"ok": True}

    desc = await _describe(mcp, "once")
    assert desc.count(_BUDGET_MARKER) == 1


# ── the once-stated affordance ───────────────────────────────────────────────


def test_affordance_states_default_and_recovery():
    aff = tool_timeout_affordance(300.0)
    assert "300" in aff
    assert "timed_out" in aff  # recovery contract spelled out
    assert ("smaller step" in aff) or ("paginate" in aff)


def test_affordance_reflects_env_tuned_default():
    # default flows from DEFAULT_TOOL_TIMEOUT_S (env CRAWLER_EXPLORER_TOOL_TIMEOUT_S);
    # a retuned value must show in the ONE statement, not a hard-coded 300.
    aff = tool_timeout_affordance(120.0)
    assert "120" in aff
    assert "300" not in aff


# ── options wiring: stated once in the system prompt, scoped to the server ────


def test_options_injects_affordance_when_browser_harness_loaded():
    from runtime.options import build_options

    built = build_options()  # default load_mcp_json=True → real .mcp.json
    assert "browser-harness" in built.options.mcp_servers
    append = built.options.system_prompt["append"]
    assert "## MCP tool timeouts" in append
    # stated once, not duplicated
    assert append.count("## MCP tool timeouts") == 1


def test_options_omits_affordance_without_browser_harness():
    from runtime.options import build_options

    built = build_options(load_mcp_json=False)  # Data-agent style: no MCP server
    append = built.options.system_prompt.get("append", "")
    assert "## MCP tool timeouts" not in append
