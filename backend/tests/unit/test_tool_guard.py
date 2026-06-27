"""Unit tests for src.agents.agentic.tool_guard — discovery agent file-tool
confinement (C1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.agentic.tool_guard import build_workspace_confinement_hooks


def _callback(ws: Path):
    hooks = build_workspace_confinement_hooks(ws)
    return hooks["PreToolUse"][0].hooks[0]


def _decision(result: dict) -> str:
    return (result.get("hookSpecificOutput") or {}).get("permissionDecision", "allow")


async def _run(cb, tool: str, tool_input: dict) -> str:
    return _decision(await cb({"tool_name": tool, "tool_input": tool_input}, None, None))


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = (tmp_path / "session").resolve()
    d.mkdir()
    return d


async def test_bash_is_denied(ws):
    cb = _callback(ws)
    assert await _run(cb, "Bash", {"command": "printenv"}) == "deny"
    assert await _run(cb, "BashOutput", {"bash_id": "x"}) == "deny"
    assert await _run(cb, "KillShell", {"shell_id": "x"}) == "deny"


async def test_read_inside_workspace_allowed(ws):
    cb = _callback(ws)
    assert await _run(cb, "Read", {"file_path": str(ws / "fetched" / "x.md")}) == "allow"
    assert await _run(cb, "Read", {"file_path": "fetched/x.md"}) == "allow"  # relative→cwd
    assert await _run(cb, "Write", {"file_path": "task_description.md"}) == "allow"


async def test_read_escape_via_dotdot_denied(ws):
    cb = _callback(ws)
    assert await _run(cb, "Read", {"file_path": "../../../.env"}) == "deny"
    assert await _run(cb, "Read", {"file_path": str(ws.parent / ".env")}) == "deny"


async def test_absolute_outside_denied(ws):
    cb = _callback(ws)
    import os

    outside = "C:/Windows/win.ini" if os.name == "nt" else "/etc/passwd"
    assert await _run(cb, "Read", {"file_path": outside}) == "deny"
    assert await _run(cb, "Edit", {"file_path": outside}) == "deny"


async def test_grep_glob_path_confined(ws):
    cb = _callback(ws)
    # Grep/Glob read content/existence — escape via `path` must be denied.
    assert await _run(cb, "Grep", {"pattern": "KEY", "path": "../../"}) == "deny"
    assert await _run(cb, "Glob", {"pattern": "*.env", "path": str(ws.parent)}) == "deny"
    # No path → defaults to cwd (the workspace) → allowed.
    assert await _run(cb, "Grep", {"pattern": "KEY"}) == "allow"
    assert await _run(cb, "Glob", {"pattern": "**/*.md"}) == "allow"


async def test_symlink_escape_denied(ws, tmp_path):
    # A symlink planted inside the workspace pointing OUT must not grant a read
    # outside it: .resolve() collapses the link, the target is outside → deny.
    secret = tmp_path / "secret.txt"
    secret.write_text("k", encoding="utf-8")
    link = ws / "link"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform/user")
    cb = _callback(ws)
    assert await _run(cb, "Read", {"file_path": "link"}) == "deny"


async def test_mcp_and_unknown_tools_not_path_constrained(ws):
    cb = _callback(ws)
    # MCP discovery tools and non-file built-ins aren't path-checked here.
    assert await _run(cb, "mcp__zillusion-discovery__search_web", {"queries": ["x"]}) == "allow"
    assert await _run(cb, "WebSearch", {"query": "x"}) == "allow"
