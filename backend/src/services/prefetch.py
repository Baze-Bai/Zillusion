"""Parallel prefetch optimization — ported from Claude Code's startup overlap pattern.

Design principle from Claude Code:
  Overlap I/O-intensive operations with CPU-intensive module loading.
  Start async prefetches BEFORE heavy imports so they run in parallel.

This module implements:
  1. Adapter health checks (parallel)
  2. Redis cache warmup
  3. MCP server connections
  4. Domain authority table loading
  All running concurrently during startup, not blocking first request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


async def _prefetch_adapter_health() -> dict[str, bool]:
    """Parallel health check across all registered adapters.

    Emits a single summary line — `BaseRegistryAdapter.health_check` runs
    at DEBUG so this is the only INFO log per startup that names which
    adapters were checked. Failed adapter names are listed inline so an
    ops person grepping container logs at INFO sees what's broken without
    cranking the whole app to DEBUG.
    """
    from src.adapters.registry import AdapterRegistry

    try:
        results = await AdapterRegistry.health_check_all()
        healthy = sum(1 for r in results.values() if r.is_healthy)
        total = len(results)
        failed = sorted(name for name, r in results.items() if not r.is_healthy)
        if failed:
            logger.info(
                "Adapter health: %d/%d healthy, failed: %s",
                healthy, total, ", ".join(failed),
            )
        else:
            logger.info("Adapter health: %d/%d healthy", healthy, total)
        return {name: r.is_healthy for name, r in results.items()}
    except Exception as e:
        logger.warning("Adapter health check failed: %s", e)
        return {}


async def _prefetch_cache_warmup() -> None:
    """Ping Redis to ensure connection is warm."""
    from src.services.cache import cache_service

    try:
        if cache_service._redis:
            await cache_service._redis.ping()
            logger.debug("Redis connection warm")
    except Exception as e:
        logger.debug("Redis warmup skipped: %s", e)


async def _prefetch_domain_priors() -> int:
    """Pre-load domain authority table into memory."""
    from src.judging.domain_prior_table import DOMAIN_AUTHORITY
    count = len(DOMAIN_AUTHORITY)
    logger.debug("Domain authority table: %d entries loaded", count)
    return count


async def _prefetch_portal_whitelist() -> int:
    """Pre-load portal whitelist into memory."""
    from src.classifiers.url_patterns import KNOWN_PORTALS
    count = len(KNOWN_PORTALS)
    logger.debug("Portal whitelist: %d entries loaded", count)
    return count


async def run_startup_prefetches() -> dict[str, Any]:
    """Run all startup prefetches in parallel.

    Called during FastAPI lifespan startup, BEFORE the first request.
    All operations run concurrently — total time ≈ slowest single operation.
    """
    start = time.monotonic()
    logger.info("Starting parallel prefetches...")

    results = await asyncio.gather(
        _prefetch_adapter_health(),
        _prefetch_cache_warmup(),
        _prefetch_domain_priors(),
        _prefetch_portal_whitelist(),
        return_exceptions=True,
    )

    duration = (time.monotonic() - start) * 1000
    logger.info("All prefetches completed in %.1fms", duration)

    return {
        "adapter_health": results[0] if not isinstance(results[0], Exception) else {},
        "cache_warm": not isinstance(results[1], Exception),
        "domain_priors": results[2] if not isinstance(results[2], Exception) else 0,
        "portal_whitelist": results[3] if not isinstance(results[3], Exception) else 0,
        "duration_ms": duration,
    }


async def run_deferred_prefetches() -> None:
    """Deferred prefetches — run AFTER first render/response, non-blocking.

    Ported from Claude Code's startDeferredPrefetches() pattern:
    each function is memoized, multiple calls don't repeat.
    """
    # These are non-critical and can happen in background
    tasks = [
        # TODO: MCP server auto-connect
        # TODO: Model capability refresh
        # TODO: Search engine quota check
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
