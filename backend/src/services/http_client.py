"""Instrumented HTTP client for adapters and other outbound calls.

Every call routed through here gets:
  - rate-limiting via `rate_limited_call(provider, ...)`
  - one structured log line on completion with: provider, method, host,
    path, status, duration_ms, request_size, response_size, error
  - automatic `result_count` extraction when the response is JSON and the
    caller hints at where the result list lives (`result_count_path`).

Adapters used to embed `httpx.AsyncClient` directly inside `_do_search`
closures, which made it impossible to reason about external traffic from
logs alone. Funnelling everything through one place fixes that for free.

Usage
-----
    from src.services.http_client import instrumented_get

    data = await instrumented_get(
        provider="openalex",
        url=f"{base_url}/works",
        params={"search": query, "per_page": 15},
        headers={...},
        result_count_path="results",  # data["results"] is the list
    )

The function returns the parsed JSON (or `None` on failure — adapters
historically swallow errors and return [], so we keep that contract).
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.services.rate_limiter import rate_limited_call
from src.services.run_logger import log_event

logger = logging.getLogger("http.client")


def _safe_host_path(url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(url)
        return parsed.netloc or "-", parsed.path or "/"
    except Exception:
        return "-", "/"


def _result_count(data: Any, path: str | None) -> int | None:
    """Best-effort extraction of a list length from a JSON payload."""
    if path is None or data is None:
        return None
    try:
        cursor: Any = data
        for segment in path.split("."):
            if isinstance(cursor, dict):
                cursor = cursor.get(segment)
            else:
                return None
        if isinstance(cursor, list):
            return len(cursor)
        return None
    except Exception:
        return None


async def instrumented_request(
    provider: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    json_body: Any = None,
    timeout: float = 30.0,
    result_count_path: str | None = None,
    parse_json: bool = True,
) -> Any:
    """Make a rate-limited HTTP request with structured logging.

    Returns the parsed JSON (or raw response text if parse_json=False) on
    success. Raises `httpx.HTTPError` for the caller to handle — adapters
    typically catch this and return an empty list.
    """

    host, path = _safe_host_path(url)
    method = method.upper()

    async def _do_call() -> tuple[httpx.Response, float]:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            start = time.monotonic()
            resp = await client.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
            )
            elapsed = time.monotonic() - start
            return resp, elapsed

    err_msg: str | None = None
    status: int | None = None
    duration_ms: float = 0.0
    response_size: int = 0
    result_count: int | None = None
    parsed: Any = None

    overall_start = time.monotonic()
    try:
        resp, elapsed = await rate_limited_call(provider, _do_call())
        status = resp.status_code
        duration_ms = round(elapsed * 1000, 2)
        body_bytes = resp.content or b""
        response_size = len(body_bytes)
        resp.raise_for_status()
        if parse_json:
            try:
                parsed = resp.json()
            except ValueError:
                parsed = None
                err_msg = "non_json_response"
        else:
            parsed = resp.text
        result_count = _result_count(parsed, result_count_path) if parse_json else None
        return parsed
    except httpx.HTTPStatusError as e:
        err_msg = f"http_{e.response.status_code}"
        raise
    except httpx.HTTPError as e:
        err_msg = f"{type(e).__name__}: {e}"
        raise
    except Exception as e:  # pragma: no cover — defensive
        err_msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        # Make sure we record duration even if rate-limit acquire took most
        # of the time and the actual HTTP call raised before recording.
        if duration_ms == 0.0:
            duration_ms = round((time.monotonic() - overall_start) * 1000, 2)

        log_extra = {
            "event": "http.request",
            "provider": provider,
            "method": method,
            "host": host,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
            "response_bytes": response_size,
            "result_count": result_count,
            "error": err_msg,
        }
        if err_msg:
            logger.warning("http.request.failed", extra=log_extra)
            # Structured run-log event so outbound HTTP failures (adapter
            # registries, etc.) are visible in the per-query run-log and the
            # diagnostics blob. Previously these only hit stdlib logs and were
            # invisible to anything consuming the JSONL run-log / diagnostics.
            log_event("http_error", {
                "provider": provider,
                "method": method,
                "host": host,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
                "error": err_msg,
            })
        else:
            logger.info("http.request.ok", extra=log_extra)


async def instrumented_get(
    provider: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
    result_count_path: str | None = None,
    parse_json: bool = True,
) -> Any:
    """Convenience wrapper for GET requests."""
    return await instrumented_request(
        provider=provider,
        method="GET",
        url=url,
        params=params,
        headers=headers,
        timeout=timeout,
        result_count_path=result_count_path,
        parse_json=parse_json,
    )


async def instrumented_post(
    provider: str,
    url: str,
    *,
    json_body: Any = None,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
    result_count_path: str | None = None,
    parse_json: bool = True,
) -> Any:
    """Convenience wrapper for POST requests."""
    return await instrumented_request(
        provider=provider,
        method="POST",
        url=url,
        params=params,
        headers=headers,
        json_body=json_body,
        timeout=timeout,
        result_count_path=result_count_path,
        parse_json=parse_json,
    )
