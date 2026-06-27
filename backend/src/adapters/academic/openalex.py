"""OpenAlex adapter — free, no API key required, 200M+ works."""

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

# OpenAlex `type` values that represent actual data products users can consume
# as a data source. Articles / preprints / books / dissertations / reports are
# *about* data but aren't structured downloadable datasets — surfacing them as
# `known_type=DOWNLOADABLE_FILE` causes the pipeline to ship a journal DOI to
# the user labelled as a file (with file_format=unknown, content_type=text/html),
# which is misleading. Drop those at adapter level so they don't pollute the
# candidate pool that downstream judging then has to filter back out.
_OPENALEX_DATA_PRODUCT_TYPES: frozenset[str] = frozenset({"dataset"})


class OpenAlexAdapter(BaseRegistryAdapter):
    name = "openalex"
    display_name = "OpenAlex"
    domains = ["science", "research", "medicine", "biology", "health", "ai", "ml"]
    worker_tags = ["academic"]
    rate_limit = RateLimitConfig(requests_per_second=10, burst=20)
    requires_auth = False
    base_url = "https://api.openalex.org"

    async def search(
        self, keywords: list[str], filters: dict
    ) -> list[RawCandidate]:
        query = " ".join(keywords[:5])

        ua = (
            f"DataSourceDiscoveryAgent/1.0 (mailto:{settings.adapter.openalex_email})"
            if settings.adapter.openalex_email
            else "DataSourceDiscoveryAgent/1.0"
        )
        logger.debug("OpenAlex search: query=%s, per_page=15, sort=relevance_score:desc", query[:80])
        try:
            data = await instrumented_get(
                provider="openalex",
                url=f"{self.base_url}/works",
                params={
                    "search": query,
                    "per_page": 15,
                    "sort": "relevance_score:desc",
                    "select": "id,display_name,doi,publication_date,cited_by_count,type,open_access,authorships",
                },
                headers={"User-Agent": ua},
                result_count_path="results",
            )
        except Exception as e:
            logger.warning("OpenAlex search failed: %s", e)
            return []
        if data is None:
            return []

        candidates = []
        skipped_non_data = 0
        for work in data.get("results", []):
            work_type = (work.get("type") or "").lower()
            if work_type and work_type not in _OPENALEX_DATA_PRODUCT_TYPES:
                # article / preprint / book / dissertation / report / etc. —
                # not a downloadable data source.
                skipped_non_data += 1
                continue

            url = work.get("doi", work.get("id", ""))
            if url.startswith("https://doi.org/"):
                url = url  # Keep DOI URL
            elif url.startswith("https://openalex.org/"):
                url = url
            else:
                continue

            candidates.append(
                RawCandidate(
                    url=url,
                    title=work.get("display_name", ""),
                    snippet=f"Cited by: {work.get('cited_by_count', 0)}. Type: {work.get('type', 'unknown')}",
                    source_engine="openalex",
                    discovery_method="registry",
                    known_type=DataSourceType.DOWNLOADABLE_FILE,  # Academic papers are downloadable
                    priority="normal",
                    metadata={
                        "cited_by_count": work.get("cited_by_count", 0),
                        "publication_date": work.get("publication_date"),
                        "type": work.get("type"),
                        "is_oa": work.get("open_access", {}).get("is_oa", False),
                    },
                )
            )
            logger.debug("OpenAlex work: %s (cited=%s, date=%s, type=%s, oa=%s)",
                          url[:80], work.get("cited_by_count"),
                          work.get("publication_date"), work.get("type"),
                          work.get("open_access", {}).get("is_oa"))

        if skipped_non_data:
            logger.debug(
                "OpenAlex skipped %d non-data-product works (articles/preprints/books)",
                skipped_non_data,
            )
        logger.debug("OpenAlex returned %d results for: %s", len(candidates), query[:50])
        return candidates

    def normalize(self, raw: dict) -> DataSource:
        source_id = hashlib.sha256(raw.get("id", "").encode()).hexdigest()[:16]
        return DataSource(
            id=source_id,
            name=raw.get("display_name", "Unknown"),
            provider="OpenAlex",
            url=raw.get("doi", raw.get("id", "")),
            source_type=DataSourceType.DOWNLOADABLE_FILE,
            description=f"Academic work: {raw.get('display_name', '')}",
            domain="science",
            tags=["academic", "research"],
            data_format=["pdf"],
            access_level=AccessLevel.OPEN if raw.get("open_access", {}).get("is_oa") else AccessLevel.PAYWALL,
            discovery_method="registry",
            discovered_at=datetime.utcnow(),
            metadata=raw,
        )

    def get_metadata_signals(self, raw: dict) -> dict:
        return {"citations": raw.get("cited_by_count", 0)}


# Auto-register on import
AdapterRegistry.register(OpenAlexAdapter())
