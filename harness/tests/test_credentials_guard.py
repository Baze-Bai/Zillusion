"""The PreToolUse guard that blocks Read on credentials.json.

Drives the hook script as a subprocess (stdin = the PreToolUse payload) so
the test exercises the exact path Claude Code invokes — a block is exit 2
with a BLOCKED message on stderr; everything else is a clean exit 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard_credentials_read.py"


def _run(payload: str) -> subprocess.CompletedProcess:
    # input= as text → utf-8, no BOM (a PowerShell `| python` pipe would add a
    # BOM and trip the hook's JSONDecodeError fallback — not how the harness
    # feeds it).
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _payload(file_path) -> str:
    inp = {} if file_path is None else {"file_path": file_path}
    return json.dumps({"tool_name": "Read", "tool_input": inp})


BLOCK_PATHS = [
    r"E:\h\inputs\github-84a928\credentials.json",
    r"E:\h\workspaces\s\validation\val-x\rerun\credentials.json",
    r"E:\h\workspaces\s\runs\run-y\credentials.json",
    "inputs/site/credentials.json",  # forward slashes / relative
]

ALLOW_PATHS = [
    r"E:\h\inputs\s\api_spec.json",
    r"E:\h\inputs\s\goal.md",
    r"E:\h\workspaces\s\output_sample.json",
    "my_credentials.json.example",  # name != credentials.json exactly
    "credentials.json.bak",
]


@pytest.mark.parametrize("path", BLOCK_PATHS)
def test_blocks_credentials_read(path):
    r = _run(_payload(path))
    assert r.returncode == 2, r.stdout
    assert "BLOCKED" in r.stderr
    assert "json.load" in r.stderr  # points at the right remedy


@pytest.mark.parametrize("path", ALLOW_PATHS)
def test_allows_other_reads(path):
    r = _run(_payload(path))
    assert r.returncode == 0, r.stderr


def test_allows_missing_file_path():
    assert _run(_payload(None)).returncode == 0


def test_allows_non_json_stdin():
    # A malformed payload must never wedge a tool call — fail open.
    assert _run("not json at all").returncode == 0


def test_blocks_with_bom_prefixed_payload():
    # Defensive: even if stdin arrives BOM-prefixed, the block still fires
    # (the hook strips it before json.load).
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=("﻿" + _payload(r"x\credentials.json")).encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    assert r.returncode == 2
