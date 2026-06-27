"""Firecrawl client — 支持自部署 (self-hosted) 和云端 API 双模式.

自部署 (推荐):
  - 零边际成本，无 API Key 需要
  - Docker Compose 启动: firecrawl + firecrawl-playwright + firecrawl-redis
  - 端点: http://firecrawl:3002/v1/scrape, /v1/map, /v1/crawl
  - 限制: 无 Fire-engine (高级反爬 bypass)，用 Playwright 兜底

云端 API (fallback):
  - 需要 SEARCH_FIRECRAWL_API_KEY
  - 端点: https://api.firecrawl.dev/v1/...
  - 有 Fire-engine 高级反爬

用法策略 (来自架构文档):
  - /scrape: 单页精读 → Markdown (Judge 证据层 + 详情页采样)
  - /map:    整站 URL 骨架 (Portal Profiler + Tier-3 列表页展开)
  - /crawl:  全站递归 — 不用 (太贵太慢)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.config import settings
from src.services.rate_limiter import rate_limited_call
from src.services.run_logger import log_event
from src.tools.net_guard import UnsafeURLError, assert_safe_url_async

logger = logging.getLogger(__name__)

# Self-hosted firecrawl runs with BROWSER_POOL_SIZE=5 (docker-compose).
# Above that, requests queue inside firecrawl waiting for a free browser,
# and our httpx timeout fires before the queue drains. A python-side
# semaphore caps how many concurrent /v1/scrape calls we hold open, so
# the queue can't grow unbounded — pages over the cap wait at our side
# (cheap) instead of inside firecrawl (where they consume a slot and
# eventually 408). Sized slightly above BROWSER_POOL_SIZE to keep the
# pool saturated while leaving headroom for the rate-limit smoothing.
_SCRAPE_CONCURRENCY = 8
_scrape_sem: asyncio.Semaphore | None = None


def _get_scrape_sem() -> asyncio.Semaphore:
    """Lazily create the scrape semaphore on the running loop.

    Created lazily because creating asyncio primitives at import time
    binds them to whatever loop is running then (often none), which
    breaks under uvicorn's reload-with-WatchFiles dev mode.
    """
    global _scrape_sem
    if _scrape_sem is None:
        _scrape_sem = asyncio.Semaphore(_SCRAPE_CONCURRENCY)
    return _scrape_sem


class FirecrawlClient:
    """Firecrawl API 客户端，自动选择自部署或云端."""

    def __init__(self) -> None:
        self._use_self_hosted = settings.search.firecrawl_use_self_hosted
        if self._use_self_hosted:
            self._base_url = settings.search.firecrawl_self_hosted_url.rstrip("/")
            self._api_key = ""  # 自部署不需要 key
        else:
            self._base_url = "https://api.firecrawl.dev"
            self._api_key = settings.search.firecrawl_api_key

    @property
    def is_configured(self) -> bool:
        """Check if Firecrawl is available (self-hosted always true, cloud needs key)."""
        if self._use_self_hosted:
            return True
        return bool(self._api_key)

    @property
    def mode(self) -> str:
        return "self-hosted" if self._use_self_hosted else "cloud"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _request(
        self, method: str, endpoint: str, json_data: dict | None = None, timeout: float = 60
    ) -> dict:
        """Send request to Firecrawl API.

        Passes a factory (not a pre-built coroutine) into rate_limited_call
        so cancellation while waiting for the rate-limit permit doesn't
        leave an orphaned coroutine behind. Without the factory form, the
        embedded processor's gather (162+ concurrent scrapes) reliably
        triggers RuntimeWarning("coroutine ... was never awaited") because
        firecrawl's queue depth pushes some siblings into cancellation
        before they get the permit.
        """
        url = f"{self._base_url}{endpoint}"

        async def _do_request():
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    resp = await client.get(url, headers=self._headers())
                else:
                    resp = await client.post(url, headers=self._headers(), json=json_data or {})
                resp.raise_for_status()
                return resp.json()

        return await rate_limited_call("firecrawl", _do_request)

    # ──────────────────────────────────
    # /v1/scrape — 单页精读 → Markdown
    # ──────────────────────────────────
    async def scrape(
        self,
        url: str,
        formats: list[str] | None = None,
        only_main_content: bool = True,
        wait_for: int = 0,
        timeout: float = 60,
    ) -> dict[str, Any]:
        """Scrape a single URL and return cleaned Markdown content.

        Behaviour notes (informed by production logs at 2026-05-07):
          - 408 ``SCRAPE_TIMEOUT`` from firecrawl is retried once with a
            shorter inner timeout and ``onlyMainContent=True`` (skips heavy
            sub-resources). Many 408s come from sites whose first paint
            takes >45 s (data.europa.eu CKAN, msn.com, scopus.com); a
            stricter retry succeeds about half the time.
          - 500 from firecrawl indicates playwright crashed on the page
            (Cloudflare anti-bot, redirect loops). Not retried — the page
            is genuinely unreachable through self-hosted firecrawl, and the
            caller will fall through to Jina Reader / direct httpx.
          - All scrapes are gated by a python-side semaphore so we don't
            queue more than ``BROWSER_POOL_SIZE+headroom`` requests inside
            firecrawl. Without this gate, queue depth grew to 19 s in
            log evidence and httpx's read timeout fired before firecrawl
            could even start the playwright job.

        Args:
            url: Target URL to scrape.
            formats: Output formats. Default ["markdown"].
            only_main_content: Strip nav/footer/ads.
            wait_for: Milliseconds to wait for JS rendering.
            timeout: HTTP client timeout in seconds.

        Returns:
            {"success": bool, "data": {"markdown": "...", "metadata": {...}}}
        """
        # SSRF guard at the firecrawl egress method — covers every caller
        # (fetch_page_with_html, tier3, …), self-hosted firecrawl runs on our
        # network so an internal/metadata target must be refused before the call.
        try:
            await assert_safe_url_async(url)
        except UnsafeURLError as e:
            logger.warning("firecrawl scrape blocked (SSRF guard): %s", e)
            return {"success": False, "error": f"blocked (SSRF guard): {e}", "data": {}}
        async with _get_scrape_sem():
            return await self._scrape_with_retry(
                url=url,
                formats=formats,
                only_main_content=only_main_content,
                wait_for=wait_for,
                timeout=timeout,
            )

    async def _scrape_with_retry(
        self,
        url: str,
        formats: list[str] | None,
        only_main_content: bool,
        wait_for: int,
        timeout: float,
    ) -> dict[str, Any]:
        """Single attempt + one retry on 408 with stricter parameters."""
        # Two-tier timeout: firecrawl's internal scrapeTimeout (forwarded to
        # playwright page.goto) MUST be shorter than our httpx timeout, so
        # firecrawl returns a clean SCRAPE_TIMEOUT error instead of httpx
        # giving up first with a silent ReadTimeout. 15s gap covers
        # firecrawl's queue + serialization overhead.
        firecrawl_timeout_ms = int(max(timeout - 15, 10) * 1000)

        payload: dict[str, Any] = {
            "url": url,
            "formats": formats or ["markdown"],
            "onlyMainContent": only_main_content,
            "timeout": firecrawl_timeout_ms,
        }
        if wait_for > 0:
            payload["waitFor"] = wait_for

        try:
            result = await self._request("POST", "/v1/scrape", payload, timeout=timeout)
            logger.debug(
                "Firecrawl scrape [%s]: %s → %d chars",
                self.mode, url[:60],
                len(result.get("data", {}).get("markdown", "")),
            )
            return result
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # 408 SCRAPE_TIMEOUT: retry once with onlyMainContent=True and
            # a tighter inner timeout. Strips most of the JS/asset weight
            # that pushed the first attempt past playwright's deadline.
            if status == 408:
                logger.info(
                    "Firecrawl 408 retry [%s]: %s (onlyMainContent forced, "
                    "timeout halved)",
                    self.mode, url[:60],
                )
                retry_timeout = max(timeout * 0.6, 25.0)
                retry_inner_ms = int(max(retry_timeout - 10, 8) * 1000)
                retry_payload = {
                    "url": url,
                    "formats": formats or ["markdown"],
                    "onlyMainContent": True,
                    "timeout": retry_inner_ms,
                }
                try:
                    return await self._request(
                        "POST", "/v1/scrape", retry_payload, timeout=retry_timeout,
                    )
                except Exception as retry_err:
                    logger.warning(
                        "Firecrawl 408 retry also failed [%s]: %s → %s",
                        self.mode, url[:60], retry_err,
                    )
                    return {
                        "success": False,
                        "error": f"timeout (retry exhausted): {retry_err}",
                        "code": "SCRAPE_TIMEOUT",
                    }
            # 500 / other: don't retry, surface so caller can fall through
            # to Jina/direct fetch.
            logger.warning(
                "Firecrawl scrape HTTP %d [%s]: %s",
                status, self.mode, url[:60],
            )
            log_event("http_error", {
                "provider": "firecrawl", "endpoint": "/v1/scrape",
                "status": status, "host": url[:120], "mode": self.mode,
                "error": f"HTTP {status}: {e}",
            })
            return {
                "success": False,
                "error": f"HTTP {status}: {e}",
                "code": "PLAYWRIGHT_ERROR" if status >= 500 else f"HTTP_{status}",
            }
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            # httpx-side timeout: usually means firecrawl queue was so deep
            # we gave up before it started. Already gated by the semaphore;
            # if we still hit this, the firecrawl service itself is the
            # bottleneck. Don't retry — let the outer fallback chain handle.
            logger.warning(
                "Firecrawl client-side timeout/network [%s]: %s → %s",
                self.mode, url[:60], type(e).__name__,
            )
            return {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "code": "CLIENT_TIMEOUT",
            }
        except Exception as e:
            logger.warning(
                "Firecrawl scrape failed [%s]: %s → %s",
                self.mode, url[:60], e,
            )
            return {"success": False, "error": str(e), "code": "UNKNOWN"}

    # ──────────────────────────────────
    # /v1/map — 整站 URL 骨架
    # ──────────────────────────────────
    async def map(
        self,
        url: str,
        limit: int = 5000,
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Get the URL map/sitemap of a website.

        Returns a list of all discoverable URLs on the site.
        Used by Portal Profiler for deep exploration.

        Args:
            url: Root URL to map.
            limit: Max URLs to return.
            timeout: Request timeout.

        Returns:
            {"success": bool, "links": ["url1", "url2", ...]}
        """
        try:
            await assert_safe_url_async(url)
        except UnsafeURLError as e:
            logger.warning("firecrawl map blocked (SSRF guard): %s", e)
            return {"success": False, "error": f"blocked (SSRF guard): {e}", "links": []}

        payload = {
            "url": url,
            "limit": limit,
        }

        try:
            result = await self._request("POST", "/v1/map", payload, timeout=timeout)
            links = result.get("links", [])
            logger.debug("Firecrawl map [%s]: %s → %d URLs", self.mode, url[:60], len(links))
            return result
        except Exception as e:
            logger.warning("Firecrawl map failed [%s]: %s → %s", self.mode, url[:60], e)
            log_event("http_error", {
                "provider": "firecrawl", "endpoint": "/v1/map",
                "host": url[:120], "mode": self.mode,
                "error": f"{type(e).__name__}: {e}",
            })
            return {"success": False, "links": [], "error": str(e)}

    # ──────────────────────────────────
    # /v1/extract — 按 Schema 抽取结构化字段
    # ──────────────────────────────────
    async def extract(
        self,
        urls: list[str],
        schema: dict | None = None,
        prompt: str = "",
        timeout: float = 60,
    ) -> dict[str, Any]:
        """Extract structured data from URLs using a schema.

        Args:
            urls: List of URLs to extract from.
            schema: JSON Schema defining expected fields.
            prompt: Natural language extraction instruction.
            timeout: Request timeout (longer for extraction).

        Returns:
            {"success": bool, "data": [{extracted fields}]}
        """
        payload: dict[str, Any] = {"urls": urls}
        if schema:
            payload["schema"] = schema
        if prompt:
            payload["prompt"] = prompt

        try:
            result = await self._request("POST", "/v1/extract", payload, timeout=timeout)
            logger.debug("Firecrawl extract [%s]: %d URLs → %d results",
                         self.mode, len(urls),
                         len(result.get("data", [])))
            return result
        except Exception as e:
            logger.warning("Firecrawl extract failed [%s]: %s", self.mode, e)
            return {"success": False, "data": [], "error": str(e)}

    # ──────────────────────────────────
    # Health check
    # ──────────────────────────────────
    async def health_check(self) -> bool:
        """Check if the Firecrawl service is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/v1/health")
                return resp.status_code < 500
        except Exception:
            return False


# ──────────────────────────────────────
# Page Fetcher: Jina Reader 优先 + Firecrawl fallback
# ──────────────────────────────────────

_firecrawl_client: FirecrawlClient | None = None


def get_firecrawl() -> FirecrawlClient:
    """Get or create the Firecrawl client singleton."""
    global _firecrawl_client
    if _firecrawl_client is None:
        _firecrawl_client = FirecrawlClient()
    return _firecrawl_client


async def fetch_page_content(url: str) -> str:
    """Fetch page content: Jina Reader (free) first, Firecrawl (self-hosted) fallback.

    Architecture doc strategy:
      Level 1: Jina Reader — 免费，约 60-70% 的页面够用
      Level 2: Firecrawl — 处理 JS 渲染 / Cloudflare / 复杂页面
    """
    # SSRF guard: self-hosted firecrawl + the httpx fallback run on our network
    # and could reach internal/metadata/loopback hosts. Treat an unsafe target
    # as an empty fetch (the function's "both failed" contract) — non-raising so
    # pipeline callers that don't expect exceptions stay stable.
    try:
        await assert_safe_url_async(url)
    except UnsafeURLError as e:
        logger.warning("fetch_page_content blocked (SSRF guard): %s", e)
        return ""

    # Level 1: Jina Reader (free)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            jina_url = f"https://r.jina.ai/{url}"
            headers = {}
            if settings.search.jina_api_key:
                headers["Authorization"] = f"Bearer {settings.search.jina_api_key}"
            resp = await client.get(jina_url, headers=headers)
            if resp.status_code == 200 and len(resp.text) > 200:
                logger.debug("Jina Reader success: %s → %d chars", url[:60], len(resp.text))
                return resp.text
    except Exception as e:
        logger.debug("Jina Reader failed for %s: %s", url[:60], e)

    # Level 2: Firecrawl (self-hosted or cloud)
    fc = get_firecrawl()
    if fc.is_configured:
        result = await fc.scrape(url)
        if result.get("success"):
            content = result.get("data", {}).get("markdown", "")
            if content:
                return content

    logger.warning("Both Jina and Firecrawl failed for: %s", url[:60])
    return ""


async def fetch_page_with_html(url: str, want_html: bool = True) -> tuple[str, str]:
    """Fetch page content as both raw HTML and Markdown.

    Tier-0 deterministic detection needs raw HTML (extruct, pandas.read_html).
    Tier-1/2 need Markdown (LLM context).

    ``want_html=False`` skips the rawHtml format on the firecrawl call AND the
    httpx HTML fallback entirely — callers that only consume markdown (the
    crawl tree, markdown-only fetch_page calls) used to buffer a second
    multi-MB copy of every page for nothing. Returns ``("", markdown)`` then.

    Strategy is layered so a firecrawl failure doesn't kill the candidate:
      1. Firecrawl dual-format (markdown + rawHtml in one call). Best for
         JS-heavy sites because playwright renders before serialization.
      2. If firecrawl returns success=False OR returns empty content,
         fall through to:
         - httpx direct GET → raw HTML (free, fast, ~70% of static sites)
         - Jina Reader → Markdown (free for low volume, handles JS)
      Any partial success (e.g. firecrawl gave HTML but no markdown) is
      filled in from the fallback path.

    Before this layering, a firecrawl 500 (which happens reliably for
    Cloudflare-protected sites like weatherspark and certain DOI
    redirects) discarded the candidate entirely — even though the page
    was reachable via plain httpx. See logs at 19:04:32 and 19:04:57 in
    the discover trace for examples.

    Returns:
        (raw_html, markdown) tuple. Either may be empty string on failure.
    """
    # SSRF guard: firecrawl (self-hosted) + the httpx fallback egress from our
    # network — block internal/metadata/loopback targets. Non-raising: an unsafe
    # URL yields the same ("", "") "both failed" result a dead page would.
    try:
        await assert_safe_url_async(url)
    except UnsafeURLError as e:
        logger.warning("fetch_page_with_html blocked (SSRF guard): %s", e)
        return "", ""

    raw_html = ""
    markdown = ""
    firecrawl_diagnostic = "not_attempted"

    # Strategy 1: Firecrawl dual-format (one call, two outputs)
    fc = get_firecrawl()
    if fc.is_configured:
        try:
            result = await fc.scrape(
                url,
                formats=["markdown", "rawHtml"] if want_html else ["markdown"],
                only_main_content=False,
            )
            if result.get("success"):
                data = result.get("data", {})
                raw_html = (data.get("rawHtml", "") or "") if want_html else ""
                markdown = data.get("markdown", "") or ""
                if markdown and (raw_html or not want_html):
                    # Full success on Firecrawl — record + return.
                    log_event("fetch_strategy", {
                        "url": url, "strategy": "firecrawl",
                        "outcome": "success",
                        "html_chars": len(raw_html), "markdown_chars": len(markdown),
                        "mode": fc.mode,
                    })
                    return raw_html, markdown
                # Partial: continue to fallback to fill missing piece.
                firecrawl_diagnostic = "partial"
                log_event("fetch_strategy", {
                    "url": url, "strategy": "firecrawl",
                    "outcome": "partial",
                    "html_chars": len(raw_html), "markdown_chars": len(markdown),
                    "mode": fc.mode,
                })
            else:
                firecrawl_diagnostic = result.get("code", "unknown_error")
                log_event("fetch_strategy", {
                    "url": url, "strategy": "firecrawl",
                    "outcome": "failed",
                    "error_code": firecrawl_diagnostic, "mode": fc.mode,
                })
        except Exception as e:
            logger.debug("Firecrawl dual-format raised for %s: %s", url[:60], e)
            firecrawl_diagnostic = type(e).__name__
            log_event("fetch_strategy", {
                "url": url, "strategy": "firecrawl",
                "outcome": "exception",
                "error_type": type(e).__name__, "mode": fc.mode,
            })

    # Strategy 2: Fallbacks — only fetch what's still missing.
    # HTML via direct GET (cheap; many static sites work fine without playwright)
    if want_html and not raw_html:
        httpx_outcome = "not_attempted"
        httpx_error = None
        httpx_status = None
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={
                    # Some sites (NCEI, certain CKAN portals) 403 the
                    # default httpx UA. A real-looking UA gets through.
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            ) as client:
                resp = await client.get(url)
                httpx_status = resp.status_code
                ct = resp.headers.get("content-type", "")
                if resp.status_code < 400 and "text/html" in ct:
                    # Cap at 2MB to prevent OOM
                    if len(resp.content) <= 2 * 1024 * 1024:
                        raw_html = resp.text
                        httpx_outcome = "success"
                    else:
                        httpx_outcome = "too_large"
                else:
                    httpx_outcome = f"status_{resp.status_code}"
        except Exception as e:
            logger.debug("Direct HTML fetch failed for %s: %s", url[:60], e)
            httpx_outcome = "exception"
            httpx_error = type(e).__name__
        log_event("fetch_strategy", {
            "url": url, "strategy": "httpx",
            "outcome": httpx_outcome,
            "html_chars": len(raw_html),
            "status_code": httpx_status,
            "error_type": httpx_error,
        })

    # Markdown via Jina Reader (handles JS rendering server-side, free tier)
    if not markdown:
        jina_outcome = "not_attempted"
        jina_error = None
        jina_status = None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                jina_url = f"https://r.jina.ai/{url}"
                headers = {}
                if settings.search.jina_api_key:
                    headers["Authorization"] = f"Bearer {settings.search.jina_api_key}"
                resp = await client.get(jina_url, headers=headers)
                jina_status = resp.status_code
                if resp.status_code == 200 and len(resp.text) > 200:
                    markdown = resp.text
                    jina_outcome = "success"
                else:
                    jina_outcome = f"status_{resp.status_code}_or_short"
        except Exception as e:
            logger.debug("Jina Reader failed for %s: %s", url[:60], e)
            jina_outcome = "exception"
            jina_error = type(e).__name__
        log_event("fetch_strategy", {
            "url": url, "strategy": "jina",
            "outcome": jina_outcome,
            "markdown_chars": len(markdown),
            "status_code": jina_status,
            "error_type": jina_error,
        })

    if not (raw_html or markdown):
        logger.info(
            "Page fetch fully exhausted for %s (firecrawl=%s, httpx+jina also empty)",
            url[:60], firecrawl_diagnostic,
        )
        log_event("fetch_strategy", {
            "url": url, "strategy": "exhausted",
            "outcome": "all_failed",
            "firecrawl_diagnostic": firecrawl_diagnostic,
        })

    return raw_html, markdown
