#!/usr/bin/env python3
"""PreToolUse hook: block the Read tool on credentials.json.

An API-workflow run receives a user-supplied API key at
``inputs/<site>/credentials.json`` (with copies staged under
``validation/<run_id>/rerun/`` and ``runs/<run_id>/`` for isolated execution).
Opening any of these with the Read tool spills the key into the run-log —
every tool_result is recorded verbatim — even though the shipped artifacts
stay clean. The workflow must load the key INSIDE python (``json.load``),
where it only ever lives in the subprocess's memory; the run-log then records
the script path, not the secret.

This hook is loaded only by the explore session (it alone discovers
``.claude/settings.json``); the validator / run / data agents use a narrow
option surface without project settings, so they are unaffected.

The key's field names are documented in ``inputs/<site>/api_spec.json``'s
``credentials.shape`` block, and the ``api-probe`` skill carries a ready
``_find_credentials()`` snippet — so there is never a reason to Read the file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    # Read raw bytes + decode utf-8-sig so a BOM-prefixed payload still blocks
    # (a text-mode json.load would fail open on the BOM — wrong for a guard).
    try:
        text = sys.stdin.buffer.read().decode("utf-8-sig")
    except (UnicodeDecodeError, ValueError):
        return 0
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return 0

    try:
        name = Path(file_path).name
    except (OSError, ValueError):
        return 0

    if name != "credentials.json":
        return 0

    sys.stderr.write(
        "BLOCKED: do not Read credentials.json — the Read result is recorded in "
        "the run-log, leaking the key. Load it INSIDE python instead "
        "(json.load), e.g. the _find_credentials() snippet from the api-probe "
        "skill; the key's field names are in inputs/<site>/api_spec.json's "
        "`credentials.shape`.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
