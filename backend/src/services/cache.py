"""Three-level Redis cache service.

L1: Search API responses     TTL = 1 hour
L2: Page fetch content        TTL = 6-24 hours
L3: Judging results           TTL = 24 hours
"""

from __future__ import annotations

import hashlib
import json
import logging

import redis.asyncio as redis

from src.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    """Hierarchical cache backed by Redis."""

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    async def connect(self) -> None:
        self._redis = redis.from_url(settings.redis.url, decode_responses=True)
        # Verify the connection — `from_url` is lazy, so without a PING
        # the first cache miss is the first time we'd find out Redis is
        # down. Caller already handles failure (main.py wraps in try).
        try:
            await self._redis.ping()
            logger.info(
                "cache.connected",
                extra={"event": "cache.connected", "url": settings.redis.url},
            )
        except Exception as e:
            logger.warning(
                "cache.connect_failed",
                extra={
                    "event": "cache.connect_failed",
                    "url": settings.redis.url,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )
            self._redis = None
            raise

    async def close(self) -> None:
        if self._redis:
            try:
                await self._redis.close()
                logger.info("cache.closed", extra={"event": "cache.closed"})
            except Exception as e:
                logger.warning(
                    "cache.close_failed",
                    extra={
                        "event": "cache.close_failed",
                        "error_type": type(e).__name__,
                        "error": str(e),
                    },
                )

    def _key(self, namespace: str, *parts: str) -> str:
        raw = ":".join(parts)
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"dsa:{namespace}:{h}"

    # === L1: Search Results (TTL 1h) ===

    async def get_search(self, provider: str, query: str) -> list[dict] | None:
        if not self._redis:
            return None
        key = self._key("search", provider, query)
        data = await self._redis.get(key)
        if data:
            logger.debug("Cache hit: search/%s/%s", provider, query[:40])
            return json.loads(data)
        logger.debug("Cache miss: search/%s/%s", provider, query[:40])
        return None

    async def set_search(self, provider: str, query: str, results: list[dict]) -> None:
        if not self._redis:
            return
        key = self._key("search", provider, query)
        await self._redis.set(key, json.dumps(results), ex=settings.cache.l1_search_ttl)
        logger.debug("Cache set: search/%s/%s (%d results, ttl=%ds)",
                      provider, query[:40], len(results), settings.cache.l1_search_ttl)

    # === L2: Page Content (TTL 6-24h) ===

    async def get_page(self, url: str) -> str | None:
        if not self._redis:
            return None
        key = self._key("page", url)
        data = await self._redis.get(key)
        if data:
            logger.debug("Cache hit: page/%s (%d chars)", url[:60], len(data))
            return data
        logger.debug("Cache miss: page/%s", url[:60])
        return None

    async def set_page(self, url: str, content: str, ttl: int | None = None) -> None:
        if not self._redis:
            return
        key = self._key("page", url)
        effective_ttl = ttl or settings.cache.l2_page_ttl
        await self._redis.set(key, content, ex=effective_ttl)
        logger.debug("Cache set: page/%s (%d chars, ttl=%ds)", url[:60], len(content), effective_ttl)

    # === L3: Judging Results (TTL 24h) ===

    async def get_judging(self, requirement_hash: str, source_hash: str) -> dict | None:
        if not self._redis:
            return None
        key = self._key("judge", requirement_hash, source_hash)
        data = await self._redis.get(key)
        if data:
            logger.debug("Cache hit: judge/%s/%s", requirement_hash[:8], source_hash[:8])
            return json.loads(data)
        logger.debug("Cache miss: judge/%s/%s", requirement_hash[:8], source_hash[:8])
        return None

    async def set_judging(
        self, requirement_hash: str, source_hash: str, scores: dict
    ) -> None:
        if not self._redis:
            return
        key = self._key("judge", requirement_hash, source_hash)
        await self._redis.set(key, json.dumps(scores), ex=settings.cache.l3_judging_ttl)
        logger.debug("Cache set: judge/%s/%s scores=%s", requirement_hash[:8], source_hash[:8], scores)


# Singleton
cache_service = CacheService()
