"""Access-wall detection beyond classic login pages — login modal over content,
visible captcha, JS/Cloudflare challenge, generic verify interstitial — driven
through the real Chromium JS via set_content.

These are the soft/verification walls that the old login-only heuristic missed
(rich content + no password field at the document root) and that now must route
to human takeover.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# rich content backdrop: >2000 chars OR >30 links -> richContent True
_RICH = "<p>" + ("readable article body " * 300) + "</p>" + "".join(
    f'<a href="/a/{i}">link {i}</a>' for i in range(40)
)


async def _check(browser, html: str) -> dict:
    await browser.page.set_content(html, wait_until="domcontentloaded")
    return await browser.check_login_wall()


async def test_login_modal_over_rich_content_is_access_wall(live_browser):
    html = f"""{_RICH}
      <div role="dialog" aria-modal="true"
           style="position:fixed;top:0;left:0;width:600px;height:420px">
        <h2>Sign in to continue reading</h2>
        <form><input type="password"></form>
      </div>"""
    res = await _check(live_browser, html)
    assert res["signals"]["modal"] is True
    assert res["wall_type"] == "login_modal"
    assert res["is_access_wall"] is True
    # back-compat: the classic login-wall verdict stays False on a rich page
    assert res["is_login_wall"] is False


async def test_visible_captcha_on_thin_page_is_access_wall(live_browser):
    # about:blank#recaptcha keeps it offline; explicit size makes it "visible".
    html = '<h1>Verify access</h1><iframe src="about:blank#recaptcha" style="width:320px;height:90px"></iframe>'
    res = await _check(live_browser, html)
    assert res["signals"]["captcha"] is True
    assert res["wall_type"] == "captcha"
    assert res["is_access_wall"] is True


async def test_cloudflare_challenge_element_is_access_wall(live_browser):
    html = '<div id="challenge-form"></div><p>Checking your browser before you continue.</p>'
    res = await _check(live_browser, html)
    assert res["signals"]["challenge"] is True
    assert res["wall_type"] == "challenge"
    assert res["is_access_wall"] is True


async def test_generic_verify_text_thin_page_is_challenge(live_browser):
    res = await _check(live_browser, "<h1>Just a moment...</h1><p>Verifying you are human.</p>")
    assert res["wall_type"] == "challenge"
    assert res["is_access_wall"] is True


async def test_incidental_captcha_on_rich_page_not_a_wall(live_browser):
    # A contact-form captcha at the bottom of a readable article must NOT be
    # flagged — the data is visible; richContent suppresses the captcha bucket.
    html = f'{_RICH}<iframe src="about:blank#recaptcha" style="width:320px;height:90px"></iframe>'
    res = await _check(live_browser, html)
    assert res["signals"]["captcha"] is True
    assert res["is_access_wall"] is False
    assert res["wall_type"] == "none"


async def test_rich_page_with_small_login_link_not_a_wall(live_browser):
    html = f'<a class="login-btn" href="/login">Log in</a>{_RICH}'
    res = await _check(live_browser, html)
    assert res["is_access_wall"] is False
    assert res["wall_type"] == "none"
