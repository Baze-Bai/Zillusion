"""`save_auth` — persist the login window's session into the global store and
re-authenticate the main browser.

Two paths:
  * no active login window  -> guarded, writes nothing (pure, no browser)
  * with a login session     -> merges into the existing file (cross-site
    accumulate) and rebuilds the main context from it (real browser)

The second test fakes the login context (an object with an async
``storage_state()`` + ``close()``) so the full merge+write+reauth path runs
headlessly, without a visible window.
"""

from __future__ import annotations

import json

import pytest

from mcp_server.browser import Browser


async def test_save_auth_without_login_window_is_guarded(tmp_path):
    b = Browser(headless=True)  # never started, no login window requested
    out = tmp_path / "auth_state.json"
    res = await b.save_auth(out)
    assert res["saved"] is False
    assert "login" in res["reason"].lower()
    assert not out.exists()  # must not write a bogus/empty auth file


async def test_save_auth_merges_new_site_into_existing_store(tmp_path, make_cookie):
    # A prior site already lives in the global store.
    out = tmp_path / "auth_state.json"
    out.write_text(
        json.dumps({"cookies": [make_cookie("old", "1", "a.com")], "origins": []}),
        encoding="utf-8",
    )

    class _FakeLoginCtx:
        async def storage_state(self):  # a fresh login on a DIFFERENT site
            return {"cookies": [make_cookie("new", "2", "b.com")], "origins": []}

        async def close(self):
            pass

    b = Browser(headless=True)
    await b.ensure_started()  # real browser needed for the main-context rebuild
    b._login_context = _FakeLoginCtx()
    try:
        res = await b.save_auth(out)
        assert res["saved"] is True
        assert res["total_cookies"] == 2

        merged = json.loads(out.read_text(encoding="utf-8"))
        assert {c["domain"] for c in merged["cookies"]} == {"a.com", "b.com"}

        # Main context was rebuilt from the merged store -> now authenticated.
        names = {c["name"] for c in await b.context.cookies()}
        assert {"old", "new"} <= names
        # Login session was cleaned up.
        assert b._login_context is None
    finally:
        await b.shutdown()
