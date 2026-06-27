"""Kaggle adapter — popular dataset platform."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from src.adapters.base import BaseRegistryAdapter, RateLimitConfig
from src.adapters.registry import AdapterRegistry
from src.models.candidates import RawCandidate
from src.models.data_source import AccessLevel, DataSource, DataSourceType

logger = logging.getLogger(__name__)


class KaggleAdapter(BaseRegistryAdapter):
    name = "kaggle"
    display_name = "Kaggle"
    domains = ["ai", "ml", "finance", "health", "science", "tech"]
    worker_tags = ["datasets"]
    rate_limit = RateLimitConfig(requests_per_second=2, burst=5)
    requires_auth = True  # Needs KAGGLE_USERNAME + KAGGLE_KEY
    base_url = "https://www.kaggle.com/api/v1"

    async def search(
        self, keywords: list[str], filters: dict
    ) -> list[RawCandidate]:
        # Kaggle API requires authentication; for now, use web search fallback
        query = " ".join(keywords[:3])
        # TODO: Implement with kaggle API client when auth is configured
        logger.debug("Kaggle adapter: auth not configured, skipping for: %s", query[:50])
        return []

    def normalize(self, raw: dict) -> DataSource:
        ref = raw.get("ref", "unknown/unknown")
        source_id = hashlib.sha256(ref.encode()).hexdigest()[:16]
        return DataSource(
            id=source_id,
            name=ref.split("/")[-1],
            provider="Kaggle",
            url=f"https://www.kaggle.com/datasets/{ref}",
            source_type=DataSourceType.DOWNLOADABLE_FILE,
            description=raw.get("title", ""),
            domain="general",
            tags=raw.get("tags", []),
            data_format=["csv"],
            access_level=AccessLevel.FREE_REGISTRATION,
            discovery_method="registry",
            metadata=raw,
        )


AdapterRegistry.register(KaggleAdapter())
