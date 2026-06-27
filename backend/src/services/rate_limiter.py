"""Per-provider async rate limiting using aiolimiter.

Logging contract:
  - `rate_limit.created` (debug): a new limiter was instantiated for a
    provider. One-time event per provider.
  - `rate_limit.acquire` (debug): every acquire attempt — verbose, gated
    behind DEBUG so we don't drown info logs.
  - `rate_limit.wait`    (debug): emitted only when acquiring blocked for
    longer than `_LOG_WAIT_THRESHOLD_S`. Was INFO historically, but in
    Stage-4 the firecrawl limiter at burst=2 produces 30+ wait lines per
    /discover; the resulting INFO spam buries everything else. The signal
    (which provider throttled us) is preserved in DEBUG and recoverable
    when needed via APP_LOG_LEVEL=DEBUG.
  - `rate_limit.error`   (warning): the wrapped coroutine raised. Re-raised
    after logging so callers see the original exception.
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Union

from aiolimiter import AsyncLimiter

from src.config import settings

logger = logging.getLogger(__name__)

_limiters: dict[str, AsyncLimiter] = {}

# Anything blocked < this is just noise; above it, we want to see it in
# logs because it likely affects user-visible latency.
_LOG_WAIT_THRESHOLD_S: float = 0.05


def _get_provider_limit(provider: str) -> tuple[float, float]:
    """Get rate limit for a provider from config."""
    rate = getattr(settings.rate_limit, provider, settings.rate_limit.default_scrape)
    return (rate, 1.0)


def get_limiter(provider: str, rate_per_s: float | None = None) -> AsyncLimiter:
    """Get or create a rate limiter for the given provider.

    ``rate_per_s`` seeds the limiter on FIRST creation instead of the
    settings lookup — used for adapters that declare their own
    RateLimitConfig rather than having an entry in settings.rate_limit."""
    if provider not in _limiters:
        if rate_per_s is not None:
            rate, period = (max(rate_per_s, 0.1), 1.0)
        else:
            rate, period = _get_provider_limit(provider)
        _limiters[provider] = AsyncLimiter(rate, period)
        logger.debug(
            "rate_limit.created",
            extra={
                "event": "rate_limit.created",
                "provider": provider,
                "rate_per_s": rate,
                "period_s": period,
            },
        )
    return _limiters[provider]


async def rate_limited_call(
    provider: str,
    coro: Union[Awaitable[Any], Callable[[], Awaitable[Any]]],
    rate_per_s: float | None = None,
):
    """Execute an async operation with rate limiting for the given provider.

    Accepts either:
      - A coroutine (legacy form: `rate_limited_call(p, my_coro())`).
      - A zero-arg callable returning a coroutine (preferred form:
        `rate_limited_call(p, lambda: my_coro())` or
        `rate_limited_call(p, my_coro)`).

    The callable form is safer: if the outer task is cancelled while waiting
    for the limiter (e.g. asyncio.gather propagating CancelledError from a
    sibling), the coroutine is never created in the first place — so Python
    can't emit `RuntimeWarning: coroutine ... was never awaited` at loop
    teardown. With the legacy form, the coroutine object is constructed at
    the call site and only awaited once we get the permit; if cancellation
    happens before that, the orphaned coroutine triggers the warning. The
    embedded processor fans out 162+ concurrent firecrawl scrapes per
    discover request, so this is not theoretical — see logs around
    19:15:36 in the discover trace where the warning fires.

    On non-trivial waits (> 50 ms), emits a debug log so operators can see
    which providers are throttling — promote to INFO via APP_LOG_LEVEL=DEBUG
    if you need to capacity-plan.
    """
    limiter = get_limiter(provider, rate_per_s)
    logger.debug(
        "rate_limit.acquire",
        extra={"event": "rate_limit.acquire", "provider": provider},
    )

    acquire_start = time.monotonic()
    async with limiter:
        wait_ms = round((time.monotonic() - acquire_start) * 1000, 2)
        if wait_ms / 1000.0 >= _LOG_WAIT_THRESHOLD_S:
            logger.debug(
                "rate_limit.wait",
                extra={
                    "event": "rate_limit.wait",
                    "provider": provider,
                    "wait_ms": wait_ms,
                },
            )
        # Resolve callable→coroutine only after acquiring the permit.
        # inspect.iscoroutine catches the legacy form; everything else we
        # treat as a factory and call now.
        if inspect.iscoroutine(coro):
            awaitable = coro
        elif callable(coro):
            awaitable = coro()
        else:
            raise TypeError(
                f"rate_limited_call expected coroutine or callable, "
                f"got {type(coro).__name__}",
            )
        try:
            return await awaitable
        except Exception as e:
            logger.warning(
                "rate_limit.error",
                extra={
                    "event": "rate_limit.error",
                    "provider": provider,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )
            raise
