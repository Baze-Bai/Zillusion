"""HTTP HEAD prober for URL validation and type detection."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from src.config import settings
from src.tools.net_guard import UnsafeURLError, assert_safe_url_async

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    url: str
    is_alive: bool
    status_code: int = 0
    content_type: str | None = None
    content_length: int | None = None
    has_attachment: bool = False
    response_time_ms: float = 0.0
    redirect_url: str | None = None


# Browser-ish UA: many government / WAF-fronted hosts (NOAA NCEI, some
# Cloudflare-protected portals) return 403/405 on bare httpx default UAs but
# accept a generic Mozilla string. Liveness probing is the critical first
# gate — a false-dead candidate is silently dropped before judge ever sees
# it, so getting past WAFs matters more than ideologically pure UAs.
_PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DataDiscoveryBot/1.0; "
        "+datasource-discovery-agent)"
    ),
    "Accept": "*/*",
}

# Status codes that indicate "the server doesn't like HEAD here, try GET."
# 405 Method Not Allowed is the textbook case; 400/403/501 catch WAFs and
# legacy servers that reject HEAD with non-standard codes. We deliberately
# do NOT retry 404/410 (resource genuinely missing) or 5xx other than 501.
_HEAD_REJECTED_CODES = frozenset({400, 403, 405, 501})


async def probe_url(url: str, timeout: float | None = None) -> ProbeResult:
    """Send HEAD request to check URL liveness and metadata.

    On HEAD failure (405/403/400/501 or transport error), fall back to a
    streamed GET so we still capture status / content-type / length headers
    without downloading the body. Sites like NOAA NCEI's portal reject bare
    HEAD probes but respond cleanly to GET — without this fallback the
    candidate is marked dead and silently dropped before the LLM judge can
    score it, leaving high-relevance authoritative sources missing from the
    final report.
    """
    if timeout is None:
        timeout = settings.validation.head_probe_timeout
    start = time.monotonic()
    # SSRF guard: candidate URLs come from untrusted search results; an internal
    # / metadata / loopback target is treated as dead (dropped) rather than
    # probed. This is also the gate the agentic probe_url tool funnels through.
    try:
        await assert_safe_url_async(url)
    except UnsafeURLError as e:
        logger.debug("probe blocked (SSRF guard) for %s: %s", url[:60], e)
        return ProbeResult(url=url, is_alive=False)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            verify=settings.validation.head_probe_verify_ssl,
            headers=_PROBE_HEADERS,
        ) as client:
            method_used = "HEAD"
            try:
                response = await client.head(url)
                head_failed = response.status_code in _HEAD_REJECTED_CODES
            except Exception as head_exc:
                logger.debug("HEAD raised for %s: %s — trying GET", url[:60], head_exc)
                response = None
                head_failed = True

            if head_failed:
                # Streamed GET: response object yields headers immediately;
                # body is never read because we exit the context immediately.
                async with client.stream("GET", url) as get_resp:
                    # Capture everything we need before exiting the context;
                    # headers stay valid on the response object after close
                    # but extracting eagerly is robust to any future change.
                    method_used = "GET"
                    status_code = get_resp.status_code
                    content_type = get_resp.headers.get("content-type", "")
                    content_length = get_resp.headers.get("content-length")
                    content_disposition = get_resp.headers.get("content-disposition", "")
                    final_url = str(get_resp.url)
            else:
                status_code = response.status_code
                content_type = response.headers.get("content-type", "")
                content_length = response.headers.get("content-length")
                content_disposition = response.headers.get("content-disposition", "")
                final_url = str(response.url)

            elapsed = (time.monotonic() - start) * 1000
            redirect = final_url if final_url != url else None
            result = ProbeResult(
                url=url,
                is_alive=status_code < 400,
                status_code=status_code,
                content_type=content_type,
                content_length=int(content_length) if content_length else None,
                has_attachment="attachment" in content_disposition,
                response_time_ms=elapsed,
                redirect_url=redirect,
            )
            logger.debug(
                "%s probe: %s → %d, ct=%s, size=%s, %.0fms%s",
                method_used, url[:60], status_code,
                content_type[:40] if content_type else None,
                content_length, elapsed,
                f", redirect={redirect[:60]}" if redirect else "",
            )
            return result

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        logger.debug("Probe failed for %s: %s", url[:60], e)
        return ProbeResult(
            url=url,
            is_alive=False,
            response_time_ms=elapsed,
        )
