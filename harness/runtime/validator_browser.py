"""Playwright-based validator tools — drive + inspect the live page.

Give the validator a FLEXIBLE browser (it judges resample_match / field_semantics
by driving the page itself, not via one fixed re-fetch tool):
  - browser_goto / browser_content / browser_evaluate -> the validator navigates,
    reads HTML, and runs its OWN JS (page.evaluate is sandboxed to the page: it can
    fetch() the site's paginated API / scrape the DOM, but cannot touch host files)
  - inspect_record_html -> dimension `field_semantics` (cleaned overview + raw
    matched element for a per-record semantics judgement)

All use the validator's OWN playwright (NOT the harness browser MCP) so
verification reproduces standalone. The chromium PROCESS is pooled across
tool calls (launching one costs ~5-10s; a validation makes 2-4 calls) while
every call still opens a fresh anti-bot context, so per-call state isolation
is unchanged. Sampled HTML is saved under validation/<run_id>/samples/ —
never touching explore artifacts. Callers that own the process lifecycle
(explore loop, CLI) call shutdown_browser() at the end.

inspect_record_html follows "overview cleaned / evidence raw / no
pre-digest": `record_html` is cleaned (orientation, fits token); the matched
single element is RAW + uncapped (evidence — cleaning could strip the very
signal that 判 a selector's semantics, e.g. an svg path shape). When NO
selector is given there is no raw match, so the overview is cleaned LIGHTER
(keeps svg geometry / full attrs / more script body) since it's the sole
evidence. <script> is collapsed, never removed, so structured-data presence
(ld+json / __NEXT_DATA__) stays visible. No nearby_semantics distillation —
the LLM reads the raw and judges.

Real browser verification happens in stage-5 integration (these tools open
Chromium); here they're written + import/compile-checked.
"""

from __future__ import annotations

import os

try:
    from playwright.async_api import async_playwright
except ImportError as exc:  # pragma: no cover
    raise ImportError("playwright required for validator_browser") from exc

from runtime.validator_checks import _workspace, resolve_auth_path


# ── pooled chromium (shared across validator tool calls) ─────────────
# Each tool call used to start playwright + launch chromium + tear both down
# (~5-10s and 200-400MB per call; a validation makes 2-4 such calls, a 5-iter
# loop 10-15). The browser PROCESS is the only thing pooled — every call
# still opens a FRESH context (cookies / anti-bot state stay isolated per
# call, same as before). The pool lives for the harness process; the explore
# loop / CLI calls shutdown_browser() when the run ends.
_pw = None
_browser = None
# Persistent context+page for the validator's browser tools (browser_goto /
# content / evaluate), reused across calls so navigation state carries over.
_session: dict = {"ctx": None, "page": None}


async def _get_browser():
    """Launch-once chromium; relaunches transparently if the previous
    instance died (crash / external kill)."""
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    if _pw is None:
        _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=os.environ.get("CRAWLER_EXPLORER_HEADLESS", "1") != "0",
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    return _browser


async def shutdown_browser() -> None:
    """Close the pooled chromium + stop the playwright driver. Idempotent;
    never raises (cleanup path)."""
    global _pw, _browser
    if _session["ctx"] is not None:
        try:
            await _session["ctx"].close()
        except Exception:  # noqa: BLE001 — already-dead context is fine
            pass
        _session["ctx"] = _session["page"] = None
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:  # noqa: BLE001 — already-dead browser is fine
            pass
        _browser = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:  # noqa: BLE001
            pass
        _pw = None


# JS: clean ONE record's outerHTML for OVERVIEW (strip noise, keep semantics).
# `light` is true when NO suspect selector was given — then this cleaned
# overview is the ONLY evidence (no raw match captured), so we preserve more:
# keep svg path geometry + full attrs + more script body. In heavy mode (a
# selector IS given → raw evidence is captured separately) we shrink harder.
# style/noscript are always dropped (no field signal). <script> is NEVER fully
# removed — its tag + type stay so the LLM knows structured data (ld+json /
# __NEXT_DATA__) exists; only its body is collapsed. data: URIs (base64 blobs,
# never signal, token-heavy) are always collapsed.
_CLEAN_RECORD_JS = r"""
(args) => {
  const [rootSel, idx, light] = args;
  const node = document.querySelectorAll(rootSel)[idx];
  if (!node) return null;
  const c = node.cloneNode(true);
  c.querySelectorAll('style,noscript').forEach(e => e.remove());
  const scriptCap = light ? 1000 : 200;
  c.querySelectorAll('script').forEach(e => {
    const t = (e.textContent || '').trim();
    if (t.length > scriptCap) e.textContent = t.slice(0, scriptCap) + '…';
  });
  if (!light) c.querySelectorAll('path[d]').forEach(p => p.setAttribute('d', '…'));
  c.querySelectorAll('*').forEach(e => {
    for (const a of [...e.attributes]) {
      if (a.value && a.value.startsWith('data:')) e.setAttribute(a.name, 'data:…');
      else if (!light && a.value && a.value.length > 200) e.setAttribute(a.name, a.value.slice(0, 200) + '…');
    }
  });
  return c.outerHTML;
}
"""

# JS: matched element RAW (evidence) + immediate context. NO cleaning.
_MATCH_RAW_JS = r"""
(args) => {
  const [rootSel, idx, sel] = args;
  const node = document.querySelectorAll(rootSel)[idx];
  if (!node) return null;
  const el = node.querySelector(sel);
  if (!el) return {found: false};
  const sibs = [...(el.parentElement ? el.parentElement.children : [])]
    .filter(x => x !== el).map(x => x.outerHTML.slice(0, 500));
  return {
    found: true,
    matched_outer_html: el.outerHTML,
    parent_open_tag: el.parentElement
      ? el.parentElement.outerHTML.slice(0, el.parentElement.outerHTML.indexOf('>') + 1) : null,
    siblings_raw: sibs.slice(0, 4),
    text: el.textContent.trim().slice(0, 200),
  };
}
"""


async def _antibot_context(browser, *, storage_state: str | None = None):
    ctx_kwargs: dict = dict(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    if storage_state:
        ctx_kwargs["storage_state"] = storage_state
    ctx = await browser.new_context(**ctx_kwargs)
    page = await ctx.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        "window.chrome={runtime:{}};"
    )
    return ctx, page


def _load_selectors(site_id: str):
    """Returns (extract_js, record_locator, {field: STRICT|TOLERANT|SKIP})."""
    from mcp_server.schemas import SelectorsFile

    sf = SelectorsFile.load(_workspace(site_id) / "selectors.yaml")
    classes = {n: e.suggested_class for n, e in sf.field_stability.fields.items()}
    return sf.selectors.extract_js, sf.selectors.record_locator, classes


async def inspect_record_html(
    site_id: str,
    run_id: str,
    source_url: str,
    *,
    record_index: int = 0,
    selector: str | None = None,
    page_timeout_ms: int = 60000,
    max_record_chars: int = 20000,
) -> dict:
    """Check 7 raw material. Cleaned record_html saved to samples/; matched
    element RAW inlined (evidence). No distillation — LLM reads raw + judges."""
    ws = _workspace(site_id)
    _, record_locator, _ = _load_selectors(site_id)
    samples = ws / "validation" / run_id / "samples"
    samples.mkdir(parents=True, exist_ok=True)

    browser = await _get_browser()
    auth = resolve_auth_path()
    ctx, page = await _antibot_context(browser, storage_state=str(auth) if auth.exists() else None)
    try:
        await page.goto(source_url, wait_until="domcontentloaded", timeout=page_timeout_ms)
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        record_html = await page.evaluate(
            _CLEAN_RECORD_JS, [record_locator, record_index, selector is None]
        )
        match = (
            await page.evaluate(_MATCH_RAW_JS, [record_locator, record_index, selector])
            if selector
            else None
        )
    finally:
        await ctx.close()

    if record_html is None:
        return {"error": f"record_locator {record_locator!r} idx {record_index} not found"}
    saved = samples / f"rec{record_index}.html"
    saved.write_text(record_html, encoding="utf-8")
    rel = f"validation/{run_id}/samples/{saved.name}"
    return {
        "saved_to": rel,
        "record_chars": len(record_html),
        "truncated": len(record_html) > max_record_chars,
        "selector_match": match,  # raw matched element + context (inlined); None if no selector
        "hint": f"cleaned record at {rel}; Read+grep aria-label/role/svg to judge selector semantics",
    }


# ── flexible browser session (validator-owned page; reused across calls) ──
# Replaces the fixed resample_and_compare: the validator drives the browser
# ITSELF — goto the right url (detail page, paginated API, listing), run its OWN
# JS (page.evaluate is sandboxed to the page: it can fetch()/scrape but cannot
# touch host files), read content — then judges resample_match (and any
# live-page dim) by synthesising over what it found. One persistent context+page
# is reused so navigation carries across goto/content/evaluate; shutdown_browser
# closes it.


async def _session_page():
    """Lazily open the persistent anti-bot context+page (with the run's auth)
    for the validator's browser tools, reused across calls."""
    if _session["page"] is not None:
        return _session["page"]
    browser = await _get_browser()
    auth = resolve_auth_path()
    ctx, page = await _antibot_context(
        browser, storage_state=str(auth) if auth.exists() else None
    )
    _session["ctx"], _session["page"] = ctx, page
    return page


async def browser_goto(url: str, *, scroll: bool = False, timeout_ms: int = 60000) -> dict:
    """Navigate the validator's page to `url` (auth + anti-bot applied). Set
    scroll=true to scroll to the bottom for lazy / infinite-scroll pages."""
    page = await _session_page()
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(1000)
    if scroll:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
    return {
        "ok": True,
        "status": resp.status if resp else None,
        "url": page.url,
        "title": await page.title(),
    }


async def browser_content(
    *, url: str | None = None, selector: str | None = None, timeout_ms: int = 60000
) -> dict:
    """Return the current page's HTML (goto `url` first if given). Pass a CSS
    `selector` to scope to a sub-tree; no selector returns the whole page, capped
    (chars_total reports the true size)."""
    page = await _session_page()
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1000)
    if selector:
        el = await page.query_selector(selector)
        html = await el.inner_html() if el is not None else None
    else:
        html = await page.content()
    html = html or ""
    return {"url": page.url, "selector": selector, "chars_total": len(html), "html": html[:24000]}


async def browser_evaluate(js: str, *, url: str | None = None, timeout_ms: int = 60000) -> dict:
    """Run YOUR OWN JS on the page (goto `url` first if given) and return its
    result. The JS runs in the browser page SANDBOX — it can fetch() any URL
    (with the page's cookies/origin, e.g. re-call the site's paginated API), read
    the DOM, scroll, normalise values — but CANNOT touch host files. Return a
    JSON-serialisable value; keep it small / pre-summarise."""
    page = await _session_page()
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1000)
    result = await page.evaluate(js)
    return {"url": page.url, "result": result}


__all__ = [
    "inspect_record_html",
    "browser_goto",
    "browser_content",
    "browser_evaluate",
    "shutdown_browser",
]
