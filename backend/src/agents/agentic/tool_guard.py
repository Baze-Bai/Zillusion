"""Workspace confinement for the discovery agent's built-in file tools.

The agent runs with ``permission_mode="bypassPermissions"`` (so its many MCP +
file-tool calls don't each prompt). The cost is that the SDK's built-in
Read/Write/Edit/Grep/Glob would otherwise reach ANY path — and the agent's cwd
sits inside the backend tree, so ``../../../.env`` (every provider key) and other
sites' ``inputs/<site>/credentials.json`` are in reach. The agent is also
steerable by injected page content, making that a real exfiltration path.

A ``PreToolUse`` hook fires EVEN under bypassPermissions (the harness relies on
the same property), so this is the one mechanism that can confine the file
tools without abandoning bypass mode. It:

  - DENIES Bash/shell tools outright (the discovery agent has no task that needs
    a shell; this also removes the ``printenv`` path to the subprocess env where
    the LLM-provider key lives), and
  - confines every file-tool path argument to the session workspace subtree —
    an absolute or ``../`` path that resolves outside is denied. ``.resolve()``
    also collapses symlinks, so a symlink planted inside the workspace can't
    redirect a read outside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # the SDK is the runtime dep; keep import-time failure legible
    from claude_agent_sdk import HookMatcher
except Exception:  # pragma: no cover - exercised only without the SDK installed
    HookMatcher = None  # type: ignore[assignment]

# Shell tools: denied outright (no discovery task needs them).
_DENIED_TOOLS = {"Bash", "BashOutput", "KillShell", "KillBash"}

# Built-in tools whose path argument must stay inside the workspace. The value
# is the tuple of tool_input keys that can carry a path.
_PATH_TOOLS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("path",),
    "Glob": ("path",),
}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _within(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def _resolve(workspace: Path, raw: str) -> Path | None:
    try:
        p = Path(raw)
        if not p.is_absolute():
            p = workspace / p
        return p.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def build_workspace_confinement_hooks(workspace_dir: Path) -> dict[str, list]:
    """Return a ``hooks=`` mapping for ClaudeAgentOptions that confines the
    built-in file tools to ``workspace_dir`` and denies shell tools. Returns an
    empty mapping if the SDK's HookMatcher is unavailable (fail-open only when
    the SDK itself is absent, which means no agent runs anyway)."""
    if HookMatcher is None:  # pragma: no cover
        return {}
    root = workspace_dir.resolve()

    async def _pre_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        tool = input_data.get("tool_name", "") or ""
        if tool in _DENIED_TOOLS:
            return _deny(f"{tool} is disabled for the discovery agent.")
        keys = _PATH_TOOLS.get(tool)
        if not keys:
            return {}  # MCP tools + non-file built-ins: not path-constrained here
        ti = input_data.get("tool_input") or {}
        for key in keys:
            raw = ti.get(key)
            if not raw or not isinstance(raw, str):
                continue
            resolved = _resolve(root, raw)
            if resolved is None:
                return _deny(f"{tool}: unresolvable path {raw!r}")
            if not _within(resolved, root):
                return _deny(
                    f"{tool}: path {raw!r} is outside the session workspace "
                    f"(confined to {root.name}/)."
                )
        return {}

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[_pre_tool_use])]}


__all__ = ["build_workspace_confinement_hooks"]
