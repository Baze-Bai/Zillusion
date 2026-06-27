"""Universal per-tool timeout (``mcp_server.tool_timeout``).

A hung tool returns a timeout error, fast tools pass through, opt-out works, and
FastMCP still builds the right input schema from the wrapped handler.

Decoupled from playwright: exercises the REAL ``install_tool_timeout`` against a
faithful fake mcp (FastMCP uses ``mcp.tool`` only as a registering decorator),
plus one real-FastMCP schema check to prove signatures survive the wrap.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from mcp_server.tool_timeout import install_tool_timeout


class _FakeMcp:
    """Mimics how FastMCP uses @mcp.tool() — a decorator that registers fn."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *a: Any, **k: Any):
        def deco(fn):
            self.registered[fn.__name__] = fn
            return fn

        return deco


async def test_slow_tool_times_out():
    m = _FakeMcp()
    install_tool_timeout(m, default_s=0.1)

    @m.tool()
    async def slow(x: int) -> dict:
        await asyncio.sleep(5)
        return {"ok": True, "x": x}

    out = await m.registered["slow"](x=7)
    assert out["ok"] is False
    assert out["timed_out"] is True
    assert "slow" in out["error"]


async def test_fast_tool_passes_through():
    m = _FakeMcp()
    install_tool_timeout(m, default_s=5.0)

    @m.tool()
    async def fast(x: int) -> dict:
        return {"ok": True, "x": x}

    assert await m.registered["fast"](x=3) == {"ok": True, "x": 3}


async def test_opt_out_with_none_never_times_out():
    m = _FakeMcp()
    install_tool_timeout(m, default_s=0.05)

    @m.tool(timeout_s=None)
    async def blocking() -> dict:
        await asyncio.sleep(0.2)  # exceeds default, but opted out
        return {"ok": True}

    assert await m.registered["blocking"]() == {"ok": True}


async def test_explicit_timeout_override_applies():
    m = _FakeMcp()
    install_tool_timeout(m, default_s=5.0)

    @m.tool(timeout_s=0.1)
    async def slow_override() -> dict:
        await asyncio.sleep(5)
        return {"ok": True}

    out = await m.registered["slow_override"]()
    assert out["timed_out"] is True


async def test_signature_preserved_for_schema():
    m = _FakeMcp()
    install_tool_timeout(m, default_s=1.0)

    @m.tool()
    async def probe(url: str, depth: int = 2) -> dict:
        return {"ok": True}

    # FastMCP builds the input schema from inspect.signature(handler); the
    # wrapper must not erase it (functools.wraps -> __wrapped__).
    sig = inspect.signature(m.registered["probe"])
    assert list(sig.parameters) == ["url", "depth"]


async def test_real_fastmcp_schema_unchanged():
    # The integration clincher: a real FastMCP must still derive params from the
    # wrapped handler — including a `dict[str, Any] | None` param under
    # `from __future__ import annotations` (mirrors real server tools).
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("t")
    install_tool_timeout(mcp, default_s=1.0)

    @mcp.tool()
    async def grab(selector: str, params: dict[str, Any] | None = None, limit: int = 10) -> dict:
        return {"ok": True}

    tools = await mcp.list_tools()
    grab_tool = next(t for t in tools if t.name == "grab")
    props = grab_tool.inputSchema["properties"]
    assert "selector" in props
    assert "params" in props
    assert "limit" in props
