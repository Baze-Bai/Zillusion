"""Universal per-tool wall-clock timeout for the FastMCP browser-harness server.

Every ``@mcp.tool()`` handler is wrapped so a HUNG call — e.g. an un-timed
``page.evaluate`` against a wedged page, the failure mode that froze a prod
Ctrip run for 30+ minutes — returns a tool ERROR after ``default_s`` instead of
blocking the agent (and the whole run) forever. The agent receives a normal
tool result it can recover from rather than a silent hang.

Single choke point: ``install_tool_timeout(mcp)`` rebinds ``mcp.tool`` so EVERY
tool (current and future) is covered by construction — no per-tool edits, no
way to forget a new tool.

The budget is also DISCLOSED to the agent up front, but stated ONCE rather than
repeated on every tool: ``tool_timeout_affordance()`` is a single line the
runtime appends to the agent's system prompt (see ``runtime.options``) naming the
default budget + the recovery contract. Only a tool whose budget DIFFERS from the
default carries a per-tool note in its DESCRIPTION (``_budget_note``) — e.g. the
human-login takeover (660s) or an exempt tool. FastMCP builds the description from
the handler docstring (which ``functools.wraps`` preserves) or an explicit
``description=`` kwarg if one is passed; we append to whichever FastMCP will read.

Tools that are INTENTIONALLY long-blocking (human takeover/login waits on a
person) opt out or raise their bound via ``@mcp.tool(timeout_s=...)`` /
``@mcp.tool(timeout_s=None)``.

Note on annotations: ``functools.wraps`` keeps ``__wrapped__`` + ``__annotations__``
so FastMCP still builds each tool's input schema from the original signature.
FastMCP resolves the signature via ``inspect.signature(fn, eval_str=True)``, which
follows ``__wrapped__`` and evaluates each annotation against the ORIGINAL
function's module globals — i.e. the DEFINING module (``server.py``), NOT this
wrapper module. So a tool's param/return types must be builtins or importable in
``server.py`` (where e.g. ``Image`` is already imported); nothing extra needs to
be importable here. The current surface annotates only with builtins / ``Any``.
"""

from __future__ import annotations

import asyncio
import functools
import os
from typing import Any, Callable

# 5 min default: generous enough for any legitimate browser/workspace probe,
# tight enough that a wedged page surfaces as an error instead of an infinite
# hang. Override per-tool via @mcp.tool(timeout_s=...); env-tunable for ops
# without an image rebuild.
DEFAULT_TOOL_TIMEOUT_S = float(os.environ.get("CRAWLER_EXPLORER_TOOL_TIMEOUT_S", "300"))

# Prefix of the per-tool budget note (also the idempotency sentinel — we never
# append it twice). Only EXCEPTION tools (budget != server default) carry this;
# the default itself is stated once in the system prompt via the affordance below.
_BUDGET_MARKER = "⏱"  # ⏱


def tool_timeout_affordance(default_s: float = DEFAULT_TOOL_TIMEOUT_S) -> str:
    """The single line the runtime appends to the agent's system prompt so it
    knows the per-tool wall-clock budget UP FRONT — stated ONCE instead of
    repeated on every one of the ~40 tools. Names the default + the recovery
    contract; a tool that deviates says so in its own description (``_budget_note``)."""
    return (
        "## MCP tool timeouts\n"
        f"Every browser-harness MCP tool has a wall-clock budget of ~{default_s:.0f}s. "
        "If a call exceeds it, it is auto-cancelled and returns "
        "{ok: false, timed_out: true} — a recoverable result, not a hang; take a "
        "smaller step, add a bounded wait/poll, or paginate. A tool whose budget "
        "differs (e.g. a human-login takeover waits longer, or an exempt tool may "
        "block indefinitely) states that in its own description."
    )


def _budget_note(timeout_s: float | None) -> str:
    """Terse description suffix for a tool whose budget DIFFERS from the default
    (the default is stated once in the system prompt, not repeated per tool).
    ``timeout_s is None`` ⇒ the tool is exempt and may block indefinitely."""
    if timeout_s is None:
        return (
            f"\n\n{_BUDGET_MARKER} No wall-clock timeout — this call may block "
            "indefinitely (it waits on a human)."
        )
    return f"\n\n{_BUDGET_MARKER} Wall-clock budget ~{timeout_s:.0f}s (differs from the default)."


def install_tool_timeout(mcp: Any, default_s: float = DEFAULT_TOOL_TIMEOUT_S) -> None:
    """Rebind ``mcp.tool`` so every registered tool gets a wall-clock timeout.

    Call ONCE, right after ``FastMCP(...)`` and BEFORE any ``@mcp.tool()``
    definitions (decorators run at import time, in source order).
    """
    real_tool = mcp.tool

    def tool(*d_args: Any, timeout_s: float | None = default_s, **d_kwargs: Any):
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                if timeout_s is None:
                    return await fn(*args, **kwargs)
                try:
                    return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_s)
                except asyncio.TimeoutError:
                    return {
                        "ok": False,
                        "timed_out": True,
                        "error": (
                            f"{fn.__name__} exceeded {timeout_s:.0f}s and was cancelled — "
                            f"the page or operation is likely hung; no result was returned. "
                            f"Take a smaller step, add a bounded wait/poll, or paginate."
                        ),
                    }

            # A per-tool DESCRIPTION note ONLY when this tool's budget DIFFERS from
            # the server default — the default is stated once in the system prompt
            # (tool_timeout_affordance), so we don't repeat it on all ~40 tools.
            # description= wins over the docstring in FastMCP; append to whichever it
            # reads, guarded by the marker against double-stacking.
            if timeout_s != default_s:
                note = _budget_note(timeout_s)
                _desc = d_kwargs.get("description")
                if _desc:
                    if _BUDGET_MARKER not in _desc:
                        d_kwargs["description"] = _desc + note
                elif _BUDGET_MARKER not in (wrapped.__doc__ or ""):
                    wrapped.__doc__ = (wrapped.__doc__ or "") + note

            return real_tool(*d_args, **d_kwargs)(wrapped)

        return decorator

    mcp.tool = tool
