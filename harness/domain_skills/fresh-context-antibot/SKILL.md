# Bypass anti-bot blocks with a fresh browser context

**ID**: `fresh-context-antibot`
**When to use**: The default browser context returns 403, JS challenge pages, or "You've been blocked by network security" from the target site. Skip if the default context already loads the page normally.

## Description

Create a fresh Playwright browser context with anti-detection measures (realistic UA, viewport, locale, timezone, navigator.webdriver=false, chrome.runtime stub, navigator.plugins/languages overrides) instead of using the default attached context. Works against Reddit's anti-bot layer and likely transfers to other sites with similar browser-fingerprint-based blocking.

## Recipe (see `recipe.py`)

```python
"""Bypass anti-bot blocks by creating a fresh browser context with anti-detection measures.

Use when the default attached browser context gets blocked (403, JS challenge).
The fresh context has a realistic fingerprint that many bot-detection systems accept.
"""

from playwright.async_api import Browser, BrowserContext, Page


async def create_antibot_context(browser: Browser) -> tuple[BrowserContext, Page]:
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    page = await context.new_page()
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)
    return context, page
```
