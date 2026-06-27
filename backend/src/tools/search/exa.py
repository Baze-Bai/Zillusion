"""Exa search integration — neural/semantic search, supports find_similar."""

from __future__ import annotations

import logging

import httpx

from src.config import settings
from src.models.candidates import RawCandidate
from src.services.rate_limiter import rate_limited_call
from src.tools.search.base import BaseSearchTool

logger = logging.getLogger(__name__)

EXA_API_URL = "https://api.exa.ai/search"

# Process-wide circuit breaker: a single 402 (Payment Required) means the
# configured API key has no balance, and every subsequent call across all
# parallel queries will fail identically. Without this flag a single
# /discover request fanning 5 keyword variants × 6 workers produces 30+
# identical "402 Payment Required" warnings. Once tripped, ExaSearch
# short-circuits to []; restart the process to retry.
_EXA_DISABLED_REASON: str | None = None


def _is_payment_required(exc: BaseException) -> bool:
    """Detect a 402 across litellm/httpx exception variants."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 402
    msg = str(exc)
    return "402" in msg and "payment required" in msg.lower()


class ExaSearch(BaseSearchTool):
    name = "exa"
    max_results = 10

    def is_configured(self) -> bool:
        return bool(settings.search.exa_api_key) and _EXA_DISABLED_REASON is None

    async def search(self, query: str, max_results: int | None = None) -> list[RawCandidate]:
        global _EXA_DISABLED_REASON
        if not settings.search.exa_api_key:
            return []
        if _EXA_DISABLED_REASON is not None:
            # Tripped earlier this process — silently skip. The reason was
            # already logged once at trip time.
            return []

        num = max_results or self.max_results

        async def _do_search():
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    EXA_API_URL,
                    json={
                        "query": query,
                        "num_results": num,
                        "use_autoprompt": True,
                        "type": "neural",
                    },
                    headers={
                        "Authorization": f"Bearer {settings.search.exa_api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                return response.json()

        try:
            logger.debug("Exa search: query=%s, num=%d, type=neural, autoprompt=True", query[:80], num)
            data = await rate_limited_call("exa", _do_search())
        except Exception as e:
            if _is_payment_required(e) and _EXA_DISABLED_REASON is None:
                _EXA_DISABLED_REASON = str(e)
                logger.warning(
                    "Exa disabled for the rest of this process: 402 Payment Required "
                    "(account out of credit / billing required). Subsequent calls "
                    "will short-circuit to []. Restart to retry."
                )
            elif _EXA_DISABLED_REASON is None:
                # Non-402 failure — keep the existing warning so a real
                # outage is still visible.
                logger.warning("Exa search failed: %s", e)
            return []

        candidates = []
        for result in data.get("results", []):
            url = result.get("url", "")
            exa_score = result.get("score")
            candidates.append(
                RawCandidate(
                    url=url,
                    title=result.get("title", ""),
                    snippet=result.get("text", result.get("highlight", ""))[:500],
                    source_engine="exa",
                    discovery_method="web_search",
                    metadata={
                        "exa_score": exa_score,
                        "published_date": result.get("publishedDate"),
                    },
                )
            )
            logger.debug("Exa result: %s (score=%s, date=%s)",
                          url[:80], exa_score, result.get("publishedDate"))

        logger.debug("Exa returned %d results for: %s", len(candidates), query[:50])
        return candidates
