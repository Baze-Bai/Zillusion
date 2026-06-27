"""Slash command expansion fallback.

The Claude Code CLI handles `/foo bar baz` by reading `.claude/commands/foo.md`,
stripping frontmatter, and substituting `$ARGUMENTS` / `$1` / `$2` / ... before
sending the body to the model.

The Agent SDK *should* do the same when the system prompt uses the
`claude_code` preset, but the behaviour has shifted between SDK versions.
This module is a pure-Python re-implementation we can fall back to in two
cases:

1. The SDK version we run on doesn't expand slash commands.
2. We want to inspect / log / modify the expanded body before sending
   (handy for batch jobs that need to inject context).

Public API:
    expand(prompt, project_root) -> str
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTMATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def expand(prompt: str, project_root: Path | str) -> str:
    """Expand a `/command-name [args]` prompt into the command body.

    If `prompt` does not start with `/`, or the named command does not exist,
    the original string is returned unchanged.
    """
    if not prompt.startswith("/"):
        return prompt

    parts = prompt[1:].split(maxsplit=1)
    if not parts:
        return prompt
    cmd_name = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    cmd_file = Path(project_root) / ".claude" / "commands" / f"{cmd_name}.md"
    if not cmd_file.exists():
        return prompt

    body = cmd_file.read_text(encoding="utf-8")
    body = _FRONTMATTER.sub("", body)
    body = body.replace("$ARGUMENTS", args)

    # Positional substitutions $1 .. $9.
    arg_list = args.split() if args else []
    for i, val in enumerate(arg_list, 1):
        body = body.replace(f"${i}", val)

    return body.strip()
