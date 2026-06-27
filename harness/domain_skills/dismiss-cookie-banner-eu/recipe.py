"""Recipe: dismiss an EU-style cookie banner.

Adapt the selector list to whatever the current site uses. Reject-all is
preferred since most data targets do not need consent-gated cookies.
"""

from playwright.async_api import Page


async def dismiss_cookie_banner(page: Page) -> str | None:
    btn = page.locator("#onetrust-reject-all-handler")
    if await btn.count():
        await btn.first.click()
        return "onetrust-reject"
    btn = page.locator("#CybotCookiebotDialogBodyButtonDecline")
    if await btn.count():
        await btn.first.click()
        return "cookiebot-decline"
    for label in ("Reject all", "Decline", "Accept all"):
        btn = page.get_by_role("button", name=label, exact=False)
        if await btn.count():
            await btn.first.click()
            return f"generic:{label}"
    return None
