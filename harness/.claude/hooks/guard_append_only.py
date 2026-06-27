#!/usr/bin/env python3
"""PreToolUse hook: block direct Edit/Write/MultiEdit on append-only files.

Harness variant: both `exploration_log.md` and `helpers.py` inside
workspaces are append-only. The model is redirected to the MCP tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

APPEND_ONLY_FILES = {"exploration_log.md", "helpers.py"}
APPEND_ONLY_REDIRECT = {
    "exploration_log.md": "workspace_append_log(section=..., body=...)",
    "helpers.py": "workspace_helper_append(name=..., code=...)",
}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return 0

    try:
        p = Path(file_path).resolve()
    except OSError:
        return 0

    try:
        rel = p.relative_to(Path.cwd().resolve())
    except ValueError:
        return 0

    parts = rel.parts
    if len(parts) < 3 or parts[0] != "workspaces":
        return 0
    filename = parts[-1]
    if filename not in APPEND_ONLY_FILES:
        return 0

    redirect = APPEND_ONLY_REDIRECT[filename]
    sys.stderr.write(
        f"BLOCKED: workspaces/.../{filename} is append-only. Use the MCP tool: {redirect}\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
