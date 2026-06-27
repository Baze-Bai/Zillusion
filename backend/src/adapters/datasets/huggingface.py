"""HuggingFace Datasets adapter — best-structured dataset registry."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from src.adapters.base import BaseRegistryAdapter, RateLimitConfig
from src.adapters.registry import AdapterRegistry
from src.models.candidates import RawCandidate
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.services.http_client import instrumented_get

logger = logging.getLogger(__name__)


class HuggingFaceAdapter(BaseRegistryAdapter):
    name = "huggingface"
    display_name = "HuggingFace Datasets"
    domains = ["ai", "ml", "nlp", "cv", "science", "tech"]
    worker_tags = ["datasets"]
    rate_limit = RateLimitConfig(requests_per_second=5, burst=10)
    requires_auth = False
    base_url = "https://huggingface.co/api"

    async def search(
        self, keywords: list[str], filters: dict
    ) -> list[RawCandidate]:
        query = " ".join(keywords[:3])

        logger.debug("HuggingFace search: query=%s, limit=15, sort=downloads", query[:80])
        try:
            data = await instrumented_get(
                provider="huggingface",
                url=f"{self.base_url}/datasets",
                params={
                    "search": query,
                    "limit": 15,
                    "sort": "downloads",
                    "direction": "-1",
                },
                # HF returns a top-level list; instrumented_get's
                # result_count_path only walks dict keys, so we let
                # the count fall through (None) and rely on the post-
                # processing log below.
            )
        except Exception as e:
            logger.warning("HuggingFace search failed: %s", e)
            return []
        if data is None:
            return []

        candidates = []
        if not isinstance(data, list):
            logger.debug("HuggingFace: unexpected response type %s, expected list", type(data).__name__)
        for dataset in data if isinstance(data, list) else []:
            dataset_id = dataset.get("id", "")
            url = f"https://huggingface.co/datasets/{dataset_id}"

            candidates.append(
                RawCandidate(
                    url=url,
                    title=dataset_id,
                    snippet=f"Downloads: {dataset.get('downloads', 0)}. Tags: {', '.join(dataset.get('tags', [])[:5])}",
                    source_engine="huggingface",
                    discovery_method="registry",
                    known_type=DataSourceType.DOWNLOADABLE_FILE,
                    metadata={
                        "downloads": dataset.get("downloads", 0),
                        "likes": dataset.get("likes", 0),
                        "tags": dataset.get("tags", []),
                        "last_modified": dataset.get("lastModified"),
                    },
                )
            )
            logger.debug("HF dataset: %s (downloads=%s, likes=%s, tags=%s)",
                          dataset_id, dataset.get("downloads"), dataset.get("likes"),
                          dataset.get("tags", [])[:5])

        logger.debug("HuggingFace returned %d results for: %s", len(candidates), query[:50])
        return candidates

    def normalize(self, raw: dict) -> DataSource:
        dataset_id = raw.get("id", "unknown")
        source_id = hashlib.sha256(dataset_id.encode()).hexdigest()[:16]
        return DataSource(
            id=source_id,
            name=dataset_id,
            provider="HuggingFace",
            url=f"https://huggingface.co/datasets/{dataset_id}",
            source_type=DataSourceType.DOWNLOADABLE_FILE,
            description=f"Dataset: {dataset_id}",
            domain="ai",
            tags=raw.get("tags", [])[:10],
            data_format=["parquet", "csv", "json"],
            access_level=AccessLevel.OPEN,
            discovery_method="registry",
            metadata=raw,
        )

    def get_metadata_signals(self, raw: dict) -> dict:
        return {"downloads": raw.get("downloads", 0), "likes": raw.get("likes", 0)}


AdapterRegistry.register(HuggingFaceAdapter())
