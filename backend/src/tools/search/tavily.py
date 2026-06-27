"""Tavily search integration — agent-native, returns structured summaries."""

from __future__ import annotations

import logging

import httpx

from src.config import settings
from src.models.candidates import RawCandidate
from src.services.rate_limiter import rate_limited_call
from src.tools.search.base import BaseSearchTool

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"

# Tavily 服务端硬上限实测 ~50, 不论请求多少 (我们测过 max_results=100 仍只返 48).
# Tavily 按 调用次数 × search_depth 计费, max_results 是免费的——所以无脑请求
# 服务端上限, 把"要不要这条"的决定权交给客户端 score_threshold.
_TAVILY_HARD_CEILING = 50


class TavilySearch(BaseSearchTool):
    name = "tavily"
    # 旧的 max_results 类属性已废弃——筛选完全由 settings.search.tavily_score_threshold
    # 决定, 不设客户端上下限. 类属性保留是为了兼容 BaseSearchTool 签名.
    max_results = _TAVILY_HARD_CEILING

    def is_configured(self) -> bool:
        return bool(settings.search.tavily_api_key)

    async def search(self, query: str, max_results: int | None = None) -> list[RawCandidate]:
        """Score-filtered search. The `max_results` argument is intentionally
        ignored: Tavily charges per call (not per result), so we always pull
        the server-side ceiling and let `tavily_score_threshold` shape output.
        """
        if not self.is_configured():
            return []

        threshold = settings.search.tavily_score_threshold

        async def _do_search():
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    TAVILY_API_URL,
                    json={
                        "api_key": settings.search.tavily_api_key,
                        "query": query,
                        # Always request server ceiling — free under Tavily's
                        # per-request pricing; client-side score filter decides
                        # what survives.
                        "max_results": _TAVILY_HARD_CEILING,
                        "search_depth": settings.search.tavily_search_depth,
                        "include_raw_content": settings.search.tavily_include_raw_content,
                    },
                )
                response.raise_for_status()
                return response.json()

        try:
            logger.debug(
                "Tavily search: query=%s, depth=%s, threshold=%.2f",
                query[:80], settings.search.tavily_search_depth, threshold,
            )
            data = await rate_limited_call("tavily", _do_search())
        except Exception as e:
            logger.warning("Tavily search failed: %s", e)
            return []

        raw = data.get("results", [])
        kept = [r for r in raw if (r.get("score") or 0) >= threshold]
        kept.sort(key=lambda r: r.get("score") or 0, reverse=True)

        candidates: list[RawCandidate] = []
        for result in kept:
            url = result.get("url", "")
            tavily_score = result.get("score")
            candidates.append(
                RawCandidate(
                    url=url,
                    title=result.get("title", ""),
                    snippet=result.get("content", "")[:500],
                    source_engine="tavily",
                    discovery_method="web_search",
                    metadata={"tavily_score": tavily_score},
                )
            )
            logger.debug("Tavily kept: score=%.3f %s", tavily_score or 0, url[:80])

        logger.info(
            "Tavily score-filter [threshold>=%.2f]: %d raw → %d kept for query=%s",
            threshold, len(raw), len(kept), query[:50],
        )
        return candidates
