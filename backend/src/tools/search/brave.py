"""Brave Search API integration — independent index, anti-SEO pollution."""

from __future__ import annotations

import logging

import httpx

from src.config import settings
from src.models.candidates import RawCandidate
from src.services.rate_limiter import rate_limited_call
from src.tools.search.base import BaseSearchTool

logger = logging.getLogger(__name__)

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveSearch(BaseSearchTool):
    name = "brave"
    max_results = 10

    def is_configured(self) -> bool:
        return bool(settings.search.brave_api_key)

    async def search(self, query: str, max_results: int | None = None) -> list[RawCandidate]:
        if not self.is_configured():
            return []

        num = max_results or self.max_results

        async def _do_search():
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    BRAVE_API_URL,
                    params={"q": query, "count": num},
                    headers={
                        "X-Subscription-Token": settings.search.brave_api_key,
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
                return response.json()

        try:
            logger.debug("Brave search: query=%s, num=%d", query[:80], num)
            data = await rate_limited_call("brave", _do_search())
        except Exception as e:
            logger.warning("Brave search failed: %s", e)
            return []

        candidates = []
        for result in data.get("web", {}).get("results", []):
            url = result.get("url", "")
            candidates.append(
                RawCandidate(
                    url=url,
                    title=result.get("title", ""),
                    snippet=result.get("description", "")[:500],
                    source_engine="brave",
                    discovery_method="web_search",
                    metadata={"age": result.get("age")},
                )
            )
            logger.debug("Brave result: %s (age=%s)", url[:80], result.get("age"))

        logger.debug("Brave returned %d results for: %s", len(candidates), query[:50])
        return candidates
