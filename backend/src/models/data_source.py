"""Core DataSource model — the unified output schema across the entire pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.models.scores import SourceScores
from src.models.specs import APISpec, EmbeddedSpec, FileSpec


class DataSourceType(str, Enum):
    API = "api"
    DOWNLOADABLE_FILE = "file"
    EMBEDDED_DATA = "embedded"


class AccessLevel(str, Enum):
    OPEN = "open"
    FREE_REGISTRATION = "free_reg"
    API_KEY_FREE = "api_key_free"
    API_KEY_PAID = "api_key_paid"
    OAUTH = "oauth"
    PAYWALL = "paywall"
    UNKNOWN = "unknown"


class DataSource(BaseModel):
    """Unified data source object — the core model flowing through the entire pipeline."""

    # === Identity ===
    id: str = Field(description="Globally unique ID (UUID or hash)")
    name: str
    provider: str = Field(description="Provider name, e.g. 'OpenWeatherMap', 'World Bank'")
    url: str
    source_type: DataSourceType

    # === Content Description ===
    description: str
    domain: str = Field(description="Domain tag: finance/health/geo/science/tech/...")
    tags: list[str] = Field(default_factory=list)
    data_format: list[str] = Field(default_factory=list, description="e.g. ['json', 'csv']")
    sample_excerpt: str | None = None
    estimated_record_count: int | None = None

    # === Coverage ===
    geographic_coverage: list[str] = Field(default_factory=list)
    temporal_coverage: str | None = None
    update_frequency: str | None = None

    # === Access & Legal ===
    access_level: AccessLevel = AccessLevel.UNKNOWN
    license: str | None = None
    pricing: str | None = None
    rate_limit: str | None = None

    # === Type-Specific Specs ===
    api_spec: APISpec | None = None
    file_spec: FileSpec | None = None
    embedded_spec: EmbeddedSpec | None = None

    # === Quality Scores (filled by Judge stage) ===
    scores: SourceScores | None = None

    # === Per-source usage guide (filled by Finalize stage) ===
    # The LLM-written, page-grounded "how to use this source" prose. Surfaced
    # folded into each source card in the UI (the combined per-type guide
    # strings on FinalReport are also still produced).
    usage_guide: str | None = None

    # === Provenance ===
    discovery_method: str = Field(
        description="web_search / registry / llm_prior / portal_profiler / ..."
    )
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    raw_source_category: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
