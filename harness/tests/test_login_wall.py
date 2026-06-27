"""`check_login_wall` heuristic — drives real synthetic pages through the live
Chromium JS (`_LOGIN_WALL_JS`) + the Python verdict.

The contract (browser.py:256): a page is a wall when a login SIGNAL is present
(password field / login URL / >=3 login keywords) AND it has NO rich content
(textLen>2000 or linkCount>30). The "no rich content" guard is what stops a
normal page that merely offers a "Sign in" link from being misjudged.

`set_content` leaves location at about:blank, so the URL-hit signal is exercised
separately by the live GitHub driver (its /login URL trips urlHit). Here we pin
the password-field, keyword, and rich-content-guard branches deterministically.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# ~5.4k chars of body text -> textLen > 2000 -> richContent True.
_RICH_BODY = "<p>" + ("lorem ipsum dolor sit amet " * 200) + "</p>"
_MANY_LINKS = "".join(f'<a href="/p/{i}">item {i}</a>' for i in range(40))  # >30 links


async def _verdict(browser, html: str) -> dict:
    await browser.page.set_content(html, wait_until="domcontentloaded")
    return await browser.check_login_wall()


async def test_password_form_on_thin_page_is_wall(live_browser):
    res = await _verdict(live_browser, '<h1>Sign in</h1><form><input type="password" name="pw"></form>')
    assert res["is_login_wall"] is True
    assert res["signals"]["hasPassword"] is True
    assert res["signals"]["richContent"] is False


async def test_three_plus_login_keywords_on_thin_page_is_wall(live_browser):
    # No password field, but several login keywords on an otherwise-empty page.
    html = "<p>Please log in or sign up.</p><p>New here? Register or create account.</p>"
    res = await _verdict(live_browser, html)
    assert res["is_login_wall"] is True
    assert len(res["signals"]["kwHits"]) >= 3


async def test_rich_page_with_password_field_suppressed_by_guard(live_browser):
    # Login SIGNAL present (password field) but the page is content-rich:
    # the guard must win and NOT flag it (e.g. an article with an inline
    # login widget in the sidebar).
    res = await _verdict(live_browser, f'<form><input type="password"></form>{_MANY_LINKS}{_RICH_BODY}')
    assert res["signals"]["hasPassword"] is True  # signal IS present
    assert res["signals"]["richContent"] is True  # but content is rich
    assert res["is_login_wall"] is False  # -> guard suppresses the wall verdict


async def test_rich_page_with_signin_link_only_not_wall(live_browser):
    # The docstring's headline case: a normal page whose only login affordance
    # is a "Sign in" link. One keyword, no password -> no signal at all.
    res = await _verdict(live_browser, f'<a href="/login">Sign in</a>{_RICH_BODY}')
    assert res["is_login_wall"] is False


async def test_empty_page_is_not_wall(live_browser):
    res = await _verdict(live_browser, "<html><body></body></html>")
    assert res["is_login_wall"] is False


async def test_link_heavy_login_page_at_login_url_is_wall(live_browser):
    # End-to-end regression for the Ctrip case: a REAL login page (password
    # field served AT a /login URL) buried under 60 nav/footer links. urlHit is
    # True (location is /login) so the strong signal must bypass the
    # rich-content guard. Served via route interception — no network.
    links = "".join(f'<a href="/n/{i}">n{i}</a>' for i in range(60))  # >30 links
    body = f"<form><input type='password'></form>{links}"
    await live_browser.page.route(
        "**/login",
        lambda route: route.fulfill(content_type="text/html", body=body),
    )
    await live_browser.page.goto("https://example.test/login", wait_until="domcontentloaded")
    res = await live_browser.check_login_wall()
    assert res["signals"]["hasPassword"] is True
    assert res["signals"]["urlHit"] is True
    assert res["signals"]["richContent"] is True  # >30 links would normally suppress
    assert res["is_login_wall"] is True  # but password+login-URL is conclusive
