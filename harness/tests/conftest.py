"""Shared fixtures + isolation for the login / human-takeover test suite.

``mcp_server.server`` computes its module-level paths (PROJECT_ROOT,
WORKSPACES_ROOT, MEMORY_ROOT, SKILLS_ROOT, GLOBAL_AUTH) from the
``CRAWLER_EXPLORER_ROOT`` env var **at import time**. We pin it to a throwaway
temp dir here — conftest is imported before any test module — so importing the
server never touches (or authenticates against) the real project tree.

Also clears ``CRAWLER_EXPLORER_ACTIVE_SITE`` so an autonomous flag leaking in
from the parent shell can't flip the interactive/autonomous branch under us
(the dedicated test sets it explicitly via monkeypatch).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# --- import-time isolation (must run before any `import mcp_server.server`) ---
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="harness-auth-tests-"))
os.environ["CRAWLER_EXPLORER_ROOT"] = str(_TEST_ROOT)
os.environ.pop("CRAWLER_EXPLORER_ACTIVE_SITE", None)


def _cookie(name: str, value: str, domain: str, path: str = "/") -> dict:
    """A Playwright-complete session cookie (accepted as storage_state input)."""
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": -1,
        "httpOnly": False,
        "secure": False,
        "sameSite": "Lax",
    }


@pytest.fixture
def make_cookie():
    return _cookie


@pytest.fixture
def write_storage_state(tmp_path):
    """Factory: write a valid storage_state JSON file, return its path."""

    def _write(cookies: list[dict], origins: list[dict] | None = None, name: str = "auth_state.json") -> Path:
        p = tmp_path / name
        p.write_text(
            json.dumps({"cookies": cookies, "origins": origins or []}),
            encoding="utf-8",
        )
        return p

    return _write


@pytest.fixture
async def live_browser():
    """A started, headless Browser; torn down after the test."""
    from mcp_server.browser import Browser

    b = Browser(headless=True)
    await b.ensure_started()
    try:
        yield b
    finally:
        await b.shutdown()
