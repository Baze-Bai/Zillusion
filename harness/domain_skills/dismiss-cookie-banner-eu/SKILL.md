# Dismiss EU-style cookie banner

**ID**: `dismiss-cookie-banner-eu`
**When to use**: First page load lands on a GDPR/CCPA cookie banner that
covers part of the viewport and may block clicks.

## Description

Most EU sites use one of a handful of consent management platforms
(OneTrust, Cookiebot, TrustArc, Quantcast). The "accept all" or
"reject all" button usually carries a recognisable id or accessible name.
Try rejection first; falling back to acceptance is fine since we are
read-only crawlers.

Strategy:

1. Check whether the banner is present. If absent, no-op.
2. Try clicking "reject all" by known id.
3. Fall back to "accept all" by accessible name.
4. Wait for the overlay to detach before continuing.

## Evidence

Seed entry shipped with the library. Re-validate per site; selectors drift.

## Recipe (see `recipe.py`)

```python
async def dismiss_cookie_banner(page):
    btn = page.locator('#onetrust-reject-all-handler')
    if await btn.count():
        await btn.first.click()
        return "onetrust-reject"
    btn = page.locator('#CybotCookiebotDialogBodyButtonDecline')
    if await btn.count():
        await btn.first.click()
        return "cookiebot-decline"
    for label in ("Reject all", "Decline", "Accept all"):
        btn = page.get_by_role("button", name=label, exact=False)
        if await btn.count():
            await btn.first.click()
            return f"generic:{label}"
    return None
```
