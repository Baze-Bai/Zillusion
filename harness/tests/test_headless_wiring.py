"""Headed/headless wiring for the explore + run browsers.

The MCP browser-harness server's env is built from .mcp.json (NOT os.environ),
so its CRAWLER_EXPLORER_HEADLESS must be sourced from the container env — else a
headed deploy (backend forwards CRAWLER_EXPLORER_HEADLESS=0 via docker -e) would
be silently overridden back to headless by the setdefault. These pin that.
"""

from __future__ import annotations

from pathlib import Path

import runtime.options as options

_ROOT = Path(__file__).resolve().parents[1]  # harness root (has .mcp.json)


def test_headless_flag_propagates_from_env(monkeypatch):
    monkeypatch.setenv("CRAWLER_EXPLORER_HEADLESS", "0")
    srv = options._load_mcp_servers(_ROOT)
    assert srv["browser-harness"]["env"]["CRAWLER_EXPLORER_HEADLESS"] == "0"


def test_defaults_headless_when_env_unset(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_HEADLESS", raising=False)
    srv = options._load_mcp_servers(_ROOT)
    assert srv["browser-harness"]["env"]["CRAWLER_EXPLORER_HEADLESS"] == "1"
