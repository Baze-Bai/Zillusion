"""`_login_wall_verdict` — pure verdict over raw login-wall signals.

Exhaustive, no browser. Pins the strong-signal bypass (password field AT a
login URL is conclusive — flags a wall even on a link-heavy page like Ctrip's,
which has 500+ links and would otherwise trip the rich-content guard) while
preserving the guard for weaker signals.
"""

from __future__ import annotations

from mcp_server.browser import _login_wall_verdict


def _sig(hasPassword=False, urlHit=False, kw=0, textLen=100, linkCount=5):
    return {
        "hasPassword": hasPassword,
        "urlHit": urlHit,
        "kwHits": ["x"] * kw,
        "textLen": textLen,
        "linkCount": linkCount,
        "richContent": textLen > 2000 or linkCount > 30,
    }


# --- strong signal: password field + login URL -> wall regardless of content --
def test_password_plus_login_url_is_wall_even_link_heavy():
    # The Ctrip case: password + /login URL + 576 links.
    assert _login_wall_verdict(_sig(hasPassword=True, urlHit=True, linkCount=576)) is True


def test_password_plus_login_url_is_wall_even_text_heavy():
    assert _login_wall_verdict(_sig(hasPassword=True, urlHit=True, textLen=5000)) is True


def test_password_plus_login_url_thin_page_is_wall():
    assert _login_wall_verdict(_sig(hasPassword=True, urlHit=True)) is True


# --- guard still applies to weaker signals -----------------------------------
def test_password_alone_on_rich_page_suppressed():
    # Inline login widget on a content page (no login URL) -> not a wall.
    assert _login_wall_verdict(_sig(hasPassword=True, linkCount=200)) is False


def test_login_url_alone_on_rich_page_suppressed():
    assert _login_wall_verdict(_sig(urlHit=True, linkCount=200)) is False


def test_three_keywords_on_rich_page_suppressed():
    assert _login_wall_verdict(_sig(kw=3, linkCount=200)) is False


# --- weak signal on a thin page is still a wall ------------------------------
def test_password_alone_thin_page_is_wall():
    assert _login_wall_verdict(_sig(hasPassword=True)) is True


def test_three_keywords_thin_page_is_wall():
    assert _login_wall_verdict(_sig(kw=3)) is True


def test_two_keywords_thin_page_not_wall():
    # below the >=3 keyword threshold, no other signal
    assert _login_wall_verdict(_sig(kw=2)) is False


def test_no_signal_not_wall():
    assert _login_wall_verdict(_sig()) is False


# --- _access_wall_verdict: classification + priority -------------------------
from mcp_server.browser import _access_wall_verdict  # noqa: E402


def test_access_login_ranks_first():
    # a real login page that also shows a captcha -> labelled login (user logs in)
    assert _access_wall_verdict(_sig(hasPassword=True, urlHit=True) | {"captcha": True}) == (True, "login")


def test_access_challenge():
    assert _access_wall_verdict(_sig() | {"challenge": True}) == (True, "challenge")


def test_access_captcha_only_when_not_rich():
    assert _access_wall_verdict(_sig(linkCount=5) | {"captcha": True}) == (True, "captcha")
    # incidental captcha on a readable (rich) page is NOT a wall
    assert _access_wall_verdict(_sig(linkCount=200) | {"captcha": True}) == (False, "none")


def test_access_login_modal_over_rich_content():
    assert _access_wall_verdict(_sig(linkCount=200) | {"modal": True}) == (True, "login_modal")


def test_access_none_when_no_signal():
    assert _access_wall_verdict(_sig()) == (False, "none")
