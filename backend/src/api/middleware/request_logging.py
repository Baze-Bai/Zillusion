"""HTTP request/response logging middleware.

For every request:
  1. Generate or reuse `X-Request-ID` (callers may pass their own for
     cross-service tracing).
  2. Bind it to the `request_id` ContextVar — every log record produced
     during this request will inherit the id automatically.
  3. Log a `request.start` line when we begin, and a `request.end` line
     once the response is written, including method, path, status, and
     duration_ms. Failures get `request.error` + stack trace.
  4. Echo the id back as `X-Request-ID` so clients (and future hops) can
     correlate.

This is implemented as a Starlette `BaseHTTPMiddleware` subclass — the
simplest path that still gives us access to the response object after the
inner handler has produced it.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.utils.request_context import (
    new_request_id,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger("api.request")

# Endpoints we don't want to spam logs with on every health probe / heartbeat.
_QUIET_PATHS: frozenset[str] = frozenset({"/api/v1/health", "/health"})


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Mint a request_id, log start/end, surface request_id in headers."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Honour an inbound X-Request-ID for distributed tracing scenarios.
        inbound = request.headers.get("x-request-id", "").strip()
        request_id = inbound or new_request_id()
        token = set_request_id(request_id)

        # Stash on request.state in case route handlers want to access it
        # without importing the contextvar.
        request.state.request_id = request_id

        path = request.url.path
        method = request.method
        client = request.client.host if request.client else "-"
        is_quiet = path in _QUIET_PATHS

        start = time.monotonic()
        if not is_quiet:
            logger.info(
                "request.start",
                extra={
                    "event": "request.start",
                    "method": method,
                    "path": path,
                    "client": client,
                    "query": request.url.query or "",
                },
            )

        status_code = 500  # default in case handler raises before producing one
        response: Response | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            # Re-raise so FastAPI's own exception handlers still run; we just
            # ensure there's a log line carrying the request_id and stack.
            logger.exception(
                "request.error",
                extra={
                    "event": "request.error",
                    "method": method,
                    "path": path,
                    "client": client,
                },
            )
            raise
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            level = logging.INFO if status_code < 400 else (
                logging.WARNING if status_code < 500 else logging.ERROR
            )
            if not is_quiet or status_code >= 400:
                logger.log(
                    level,
                    "request.end",
                    extra={
                        "event": "request.end",
                        "method": method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": duration_ms,
                    },
                )
            if response is not None:
                response.headers["X-Request-ID"] = request_id
            reset_request_id(token)
