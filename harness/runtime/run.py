"""High-level runner: drive one `/explore <site_id>` or arbitrary prompt
through the Claude Agent SDK and stream events.

Two entry points:

    explore(site_id, ...)    - sugar around `/explore <site_id>`
    run_prompt(prompt, ...)  - any prompt, expands slash commands ourselves

Both are async generators yielding small dicts the caller can forward to a
SSE stream / DB / log. The dict shape is intentionally small and stable so
upstream code doesn't depend on the SDK's internal types.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "claude-agent-sdk is not installed. `pip install claude-agent-sdk`."
    ) from exc

from runtime import modality, pricing
from runtime.options import BuildResult, assert_api_key, build_options
from runtime.run_logger import RunLogger, jsonable_usage
from runtime.slash import expand

# Browser tool surface dropped from API-workflow explore sessions (the agent
# probes over HTTP instead). Exact mcp__<server-key>__<tool> names — the key is
# "browser-harness" per .mcp.json. The MCP server's Browser.ensure_started
# carries an env backstop (CRAWLER_EXPLORER_WORKFLOW=api) for anything missed.
BROWSER_TOOLS_DISALLOWED_FOR_API = [
    f"mcp__browser-harness__{n}"
    for n in (
        "browser_attach",
        "browser_goto",
        "browser_evaluate",
        "browser_content",
        "browser_cdp_send",
        "browser_player",
        "browser_snapshot",
        "browser_screenshot",
        "browser_vision_probe",
        "browser_check_login_wall",
        "browser_request_user_login",
        "browser_save_auth",
    )
]


def _resolve_root(project_root: Path | str | None) -> Path:
    """Same resolution rule as options.build_options."""
    return Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent


def input_workflow_type(site_id: str, project_root: Path | str | None = None) -> str:
    """Input-side workflow hint: `api` when the bridge staged an api_spec.json
    for this site, else `default`. Decides the explore session's tool surface
    BEFORE any workspace artifact exists (the workspace-side detection in
    validate/run_exec takes over once the agent writes its manifest)."""
    root = _resolve_root(project_root)
    return "api" if (root / "inputs" / site_id / "api_spec.json").exists() else "default"


@dataclass
class RunSummary:
    """End-of-run aggregate. Returned alongside the event stream."""

    site_id: str | None = None
    project_root: Path | None = None
    turn_count: int = 0
    tool_calls: int = 0
    tool_call_breakdown: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    finished: bool = False
    error: str | None = None
    run_log_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "project_root": str(self.project_root) if self.project_root else None,
            "turn_count": self.turn_count,
            "tool_calls": self.tool_calls,
            "tool_call_breakdown": dict(self.tool_call_breakdown),
            "total_cost_usd": self.total_cost_usd,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "finished": self.finished,
            "error": self.error,
            "run_log_path": self.run_log_path,
        }


async def run_prompt(
    prompt: str,
    *,
    project_root: Path | str | None = None,
    permission_mode: str = "bypassPermissions",
    max_turns: int = 80,
    model: str | None = None,
    expand_slash: bool = True,
    summary: RunSummary | None = None,
    log_role: str = "session",
    log_site: str | None = None,
    steer_queue: "asyncio.Queue | None" = None,
    disallowed_tools: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run an arbitrary prompt through the SDK; yield normalised events.

    When `steer_queue` is provided, the prompt is sent in SDK **streaming-input**
    mode (an async iterable) instead of one-shot: the initial prompt is the first
    yielded user message, then each item drained from the queue is delivered to
    the LIVE agent at its next turn boundary (mirrors the discovery runner's
    `_input_stream`). The session stays open until the iterable EXHAUSTS, so a
    `None` sentinel is pushed on the terminal ResultMessage (and again in a
    `finally`) — otherwise `query()` hangs. `steer_queue is None` (default) keeps
    the unchanged one-shot string path.

    Yielded shapes (`event["kind"]`):
        "system"          - SDK boot info (cwd, model, tool count)
        "assistant_text"  - model text chunk
        "tool_use"        - model wants to call a tool, args included
        "tool_result"     - tool result came back (stdout/stderr/error)
        "result"          - run finished, contains cost + token usage
        "error"           - exception in the loop (kind=error, error=str)

    The caller passes a `RunSummary` if they want aggregates updated in place.
    """
    assert_api_key()
    built: BuildResult = build_options(
        project_root=project_root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        disallowed_tools=disallowed_tools,
        extra_env=extra_env,
    )

    if expand_slash:
        prompt = expand(prompt, built.project_root)

    if summary is None:
        summary = RunSummary()
    summary.project_root = built.project_root

    # Per-session JSONL run log — persisted UNCONDITIONALLY under
    # workspaces/<site>/run-logs/, independent of the CLI --json flag /
    # stdout capture. This is the objective machine-readable trace the
    # auto-debug/update pipeline can read after the fact.
    rl = RunLogger(
        role=log_role,
        site_id=log_site,
        config={
            "model": model,
            "max_turns": max_turns,
            "permission_mode": permission_mode,
            "prompt_preview": prompt[:200],
            "project_root": str(built.project_root),
        },
    )
    summary.run_log_path = str(rl.path)

    def _emit(ev: dict[str, Any]) -> dict[str, Any]:
        rl.event(ev)
        return ev

    # Streaming-input mode when a steer queue is attached (live mid-run steering);
    # otherwise the unchanged one-shot string prompt.
    _steer_active = steer_queue is not None
    _input_closed = False

    def _close_input() -> None:
        nonlocal _input_closed
        if _steer_active and not _input_closed:
            _input_closed = True
            try:
                steer_queue.put_nowait(None)  # exhaust the iterable so query() returns
            except Exception:  # noqa: BLE001
                pass

    if _steer_active:
        async def _input_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
                "session_id": "",
            }
            while True:
                item = await steer_queue.get()
                if item is None:
                    return
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": str(item)},
                    "parent_tool_use_id": None,
                    "session_id": "",
                }
        prompt_arg: Any = _input_stream()
    else:
        prompt_arg = prompt

    try:
        async for msg in query(prompt=prompt_arg, options=built.options):
            if isinstance(msg, SystemMessage):
                yield _emit({
                    "kind": "system",
                    "subtype": getattr(msg, "subtype", None),
                    "data": getattr(msg, "data", {}),
                })
            elif isinstance(msg, AssistantMessage):
                summary.turn_count += 1
                # Per-turn model + token usage — the only place that reveals
                # which model actually served each turn (primary vs fallback).
                rl.write({
                    "event": "agent_turn",
                    "turn": summary.turn_count,
                    "model": getattr(msg, "model", None),
                    "usage": jsonable_usage(getattr(msg, "usage", None)),
                })
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        yield _emit({"kind": "assistant_text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        summary.tool_calls += 1
                        summary.tool_call_breakdown[block.name] = (
                            summary.tool_call_breakdown.get(block.name, 0) + 1
                        )
                        yield _emit({
                            "kind": "tool_use",
                            "name": block.name,
                            "id": block.id,
                            "input": block.input,
                        })
            elif isinstance(msg, UserMessage):
                # UserMessage carries ToolResultBlocks back from the harness.
                # Surface them so the caller can log tool outputs.
                for block in msg.content if isinstance(msg.content, list) else []:
                    if isinstance(block, ToolResultBlock):
                        yield _emit({
                            "kind": "tool_result",
                            "tool_use_id": getattr(block, "tool_use_id", None),
                            "content": getattr(block, "content", None),
                            "is_error": getattr(block, "is_error", False),
                        })
            elif isinstance(msg, ResultMessage):
                summary.finished = True
                usage = getattr(msg, "usage", {}) or {}
                # The SDK prices DeepSeek-routed tokens with Claude's table
                # (~25-40x over). Recompute from usage at DeepSeek rates when on
                # DeepSeek; keep the SDK value otherwise. See runtime/pricing.py.
                summary.total_cost_usd = pricing.effective_cost(
                    usage, getattr(msg, "total_cost_usd", None), model
                )
                summary.total_input_tokens = usage.get("input_tokens")
                summary.total_output_tokens = usage.get("output_tokens")
                yield _emit({
                    "kind": "result",
                    "cost_usd": summary.total_cost_usd,
                    "usage": usage,
                    "stop_reason": getattr(msg, "stop_reason", None),
                })
                # Agent finished this run → exhaust the streaming input so
                # query() returns (no-op in one-shot mode).
                _close_input()
        rl.write({"event": "run_complete", "summary": summary.to_dict()})
    except Exception as exc:  # noqa: BLE001 - we surface as an event
        summary.error = f"{type(exc).__name__}: {exc}"
        rl.write({
            "event": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })
        yield _emit({"kind": "error", "error": summary.error})
        raise
    finally:
        # Safety net: never leave the streaming-input iterable awaiting on a
        # background task (would hang the SDK transport teardown).
        _close_input()


async def explore(
    site_id: str,
    *,
    project_root: Path | str | None = None,
    permission_mode: str = "bypassPermissions",
    max_turns: int = 80,
    model: str | None = None,
    summary: RunSummary | None = None,
    steer_queue: "asyncio.Queue | None" = None,
) -> AsyncIterator[dict[str, Any]]:
    """Convenience: run `/explore <site_id>` through the SDK.

    Identical to `run_prompt(f"/explore {site_id}")` but tags the summary
    with the site_id for easier downstream logging. `steer_queue`, when given,
    enables live mid-run steering (see `run_prompt`).

    API-staged sites (inputs/<site>/api_spec.json present) run as the API
    Agent: the browser tool surface is dropped via disallowed_tools and the
    MCP server gets CRAWLER_EXPLORER_WORKFLOW=api (its Browser refuses to
    start under that env — the backstop). Everything else — loop, workspace
    tools, skills, steering — is the same explore session.
    """
    if summary is None:
        summary = RunSummary()
    summary.site_id = site_id

    is_api = input_workflow_type(site_id, project_root) == "api"

    # Base-model mode picks the vision tool surface: a MULTIMODAL model keeps
    # browser_screenshot (sees the page directly) and drops the redundant
    # browser_vision_probe; a TEXT model keeps browser_vision_probe (a multimodal submodel is its
    # eyes) and drops browser_screenshot. API workflows have no browser at all, so
    # both are already in the API-disallowed set. The RESOLVED mode + submodel
    # backend are forwarded to the MCP server so its backstops agree with the
    # agent-side gate. See runtime/modality.py.
    disallowed: list[str] = list(BROWSER_TOOLS_DISALLOWED_FOR_API) if is_api else []
    if not is_api:
        disallowed.extend(modality.gated_off_tools(model))

    extra_env: dict[str, str] = dict(modality.vision_env(model))
    if is_api:
        extra_env["CRAWLER_EXPLORER_WORKFLOW"] = "api"

    async for event in run_prompt(
        f"/explore {site_id}",
        project_root=project_root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        summary=summary,
        log_role="explore",
        log_site=site_id,
        steer_queue=steer_queue,
        disallowed_tools=disallowed or None,
        extra_env=extra_env or None,
    ):
        yield event
