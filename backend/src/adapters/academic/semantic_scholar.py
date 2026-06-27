"""Semantic Scholar adapter — 225M+ papers, citation graph."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from src.adapters.base import BaseRegistryAdapter, RateLimitConfig
from src.adapters.registry import AdapterRegistry
from src.config import settings
from src.models.candidates import RawCandidate
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.services.http_client import instrumented_get

logger = logging.getLogger(__name__)


class SemanticScholarAdapter(BaseRegistryAdapter):
    name = "semantic_scholar"
    display_name = "Semantic Scholar"
    domains = ["science", "research", "ai", "ml", "nlp", "cv", "medicine"]
    worker_tags = ["academic"]
    rate_limit = RateLimitConfig(requests_per_second=1, burst=1)
    requires_auth = False
    base_url = "https://api.semanticscholar.org/graph/v1"

    async def search(
        self, keywords: list[str], filters: dict
    ) -> list[RawCandidate]:
        query = " ".join(keywords[:5])

        api_key = settings.adapter.semantic_scholar_api_key
        headers = {"x-api-key": api_key} if api_key else None

        logger.debug("Semantic Scholar search: query=%s, limit=15", query[:80])
        try:
            data = await instrumented_get(
                provider="semantic_scholar",
                url=f"{self.base_url}/paper/search",
                params={
                    "query": query,
                    "limit": 15,
                    "fields": "title,url,abstract,citationCount,year,isOpenAccess,externalIds",
                },
                headers=headers,
                result_count_path="data",
            )
        except Exception as e:
            logger.warning("Semantic Scholar search failed: %s", e)
            return []
        if data is None:
            return []

        candidates = []
        for paper in data.get("data", []):
            url = paper.get("url", "")
            if not url:
                ext_ids = paper.get("externalIds", {})
                if ext_ids.get("DOI"):
                    url = f"https://doi.org/{ext_ids['DOI']}"
                else:
                    continue

            candidates.append(
                RawCandidate(
                    url=url,
                    title=paper.get("title", ""),
                    snippet=f"Citations: {paper.get('citationCount', 0)}. Year: {paper.get('year', 'unknown')}",
                    source_engine="semantic_scholar",
                    discovery_method="registry",
                    known_type=DataSourceType.DOWNLOADABLE_FILE,
                    metadata={
                        "citation_count": paper.get("citationCount", 0),
                        "year": paper.get("year"),
                        "is_open_access": paper.get("isOpenAccess", False),
                    },
                )
            )
            logger.debug("S2 paper: %s (citations=%s, year=%s, oa=%s)",
                          url[:80], paper.get("citationCount"), paper.get("year"),
                          paper.get("isOpenAccess"))

        logger.debug("Semantic Scholar returned %d results for: %s", len(candidates), query[:50])
        return candidates

    def normalize(self, raw: dict) -> DataSource:
        source_id = hashlib.sha256(str(raw.get("paperId", "")).encode()).hexdigest()[:16]
        return DataSource(
            id=source_id,
            name=raw.get("title", "Unknown"),
            provider="Semantic Scholar",
            url=raw.get("url", ""),
            source_type=DataSourceType.DOWNLOADABLE_FILE,
            description=raw.get("abstract", "")[:300],
            domain="science",
            tags=["academic", "research"],
            data_format=["pdf"],
            access_level=AccessLevel.OPEN if raw.get("isOpenAccess") else AccessLevel.UNKNOWN,
            discovery_method="registry",
            metadata=raw,
        )


AdapterRegistry.register(SemanticScholarAdapter())
