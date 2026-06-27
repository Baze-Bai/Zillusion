"""SearXNG search integration — self-hosted, aggregates 70+ engines, zero marginal cost."""

from __future__ import annotations

import logging

import httpx

from src.config import settings
from src.models.candidates import RawCandidate
from src.services.rate_limiter import rate_limited_call
from src.tools.search.base import BaseSearchTool

logger = logging.getLogger(__name__)


class SearXNGSearch(BaseSearchTool):
    name = "searxng"
    # 兼容 BaseSearchTool 签名; 实际筛选由 score_threshold + pages 决定,
    # 不设客户端上下限. SearXNG 自部署无外部 quota, 多请几页是免费的.
    max_results = 100

    def is_configured(self) -> bool:
        return bool(settings.search.searxng_url)

    async def search(self, query: str, max_results: int | None = None) -> list[RawCandidate]:
        """Multi-page score-filtered search. The `max_results` argument is
        intentionally ignored — pages are bounded by `searxng_pages` and
        relevance by `searxng_score_threshold`; output size is whatever
        passes the threshold (no min/max floor).
        """
        if not self.is_configured():
            return []

        threshold = settings.search.searxng_score_threshold
        pages = max(1, settings.search.searxng_pages)
        categories = settings.search.searxng_categories

        async def _fetch_page(page_no: int):
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{settings.search.searxng_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "pageno": page_no,
                        "categories": categories,
                    },
                )
                response.raise_for_status()
                return response.json()

        # SearXNG 限速 2 req/s (config.RATE_SEARXNG), 多页串行而不是并发——
        # 并发会被 rate_limiter 拆成串行还消耗信号量, 不如直接 for 循环清晰.
        all_results: list[dict] = []
        for p in range(1, pages + 1):
            try:
                data = await rate_limited_call("searxng", _fetch_page(p))
                page_results = data.get("results", [])
                all_results.extend(page_results)
                logger.debug("SearXNG pageno=%d: %d results", p, len(page_results))
                # 单页空结果 = 已经翻到底, 提前跳出省 rate_limit slot
                if not page_results:
                    break
            except Exception as e:
                # 单页失败不要打断已经拿到的页——降级返回前面页的结果
                logger.warning("SearXNG page %d failed: %s", p, e)
                break

        # 跨页去重 (同一 URL 在多页中出现时, 保留最高分那条)
        seen: dict[str, dict] = {}
        for r in all_results:
            url = r.get("url", "")
            if not url:
                continue
            existing = seen.get(url)
            if existing is None or (r.get("score") or 0) > (existing.get("score") or 0):
                seen[url] = r

        kept = [r for r in seen.values() if (r.get("score") or 0) >= threshold]
        kept.sort(key=lambda r: r.get("score") or 0, reverse=True)

        candidates: list[RawCandidate] = []
        for result in kept:
            url = result.get("url", "")
            engines = result.get("engines", [])
            score = result.get("score")
            candidates.append(
                RawCandidate(
                    url=url,
                    title=result.get("title", ""),
                    snippet=result.get("content", "")[:500],
                    source_engine="searxng",
                    discovery_method="web_search",
                    metadata={"engines": engines, "searxng_score": score},
                )
            )
            logger.debug("SearXNG kept: score=%.3f engines=%s %s", score or 0, engines, url[:80])

        logger.info(
            "SearXNG score-filter [pages=%d, threshold>=%.2f]: %d raw → %d unique → %d kept for query=%s",
            pages, threshold, len(all_results), len(seen), len(kept), query[:50],
        )
        return candidates
