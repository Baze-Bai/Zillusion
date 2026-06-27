"""`browser_request_user_login` tool wrapper — the autonomous safety gate.

In orchestrator-driven runs (env CRAWLER_EXPLORER_ACTIVE_SITE set) there is no
human, so the tool MUST short-circuit to {opened: false, autonomous: true}
WITHOUT launching a headed Chromium window (which would hang the run forever).
Interactively (env unset) it must delegate to the real browser method with the
login URL.

We monkeypatch the underlying ``browser.request_user_login`` so a broken gate is
caught (the spy fires) instead of actually spawning a visible window mid-test.
"""

from __future__ import annotations

import pytest

from mcp_server import server


async def test_autonomous_short_circuits_and_opens_no_window(monkeypatch):
    monkeypatch.setenv("CRAWLER_EXPLORER_ACTIVE_SITE", "some-site")

    called = {"v": False}

    async def _must_not_run(*a, **k):
        called["v"] = True  # if reached, the gate failed and a window would open
        return {"opened": True}

    monkeypatch.setattr(server._browser, "request_user_login", _must_not_run)

    res = await server.browser_request_user_login("https://example.com/login", reason="needs auth")

    assert res["opened"] is False
    assert res["autonomous"] is True
    assert "blocked" in res["reason"].lower()  # tells the agent to record a blocked hypothesis
    assert called["v"] is False  # no headed window was launched


async def test_interactive_delegates_to_browser_with_login_url(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_ACTIVE_SITE", raising=False)

    captured = {}

    async def _stub(login_url, *, seed_auth=None):
        captured["login_url"] = login_url
        captured["seed_auth"] = seed_auth
        return {"opened": True, "url": login_url}

    monkeypatch.setattr(server._browser, "request_user_login", _stub)

    res = await server.browser_request_user_login("https://example.com/login")

    assert res["opened"] is True
    assert captured["login_url"] == "https://example.com/login"
    # No global auth file exists under the temp test root, so nothing to seed.
    assert captured["seed_auth"] is None
