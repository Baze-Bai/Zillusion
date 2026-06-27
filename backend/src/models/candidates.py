"""Candidate models flowing through the pipeline stages."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.models.data_source import DataSourceType


class RawCandidate(BaseModel):
    """Output of Discovery stage (Stage 3) — raw search result before classification."""

    url: str
    title: str = ""
    snippet: str = ""
    source_engine: str = Field(
        description="Which search engine or registry found this: exa/brave/tavily/searxng/openalex/..."
    )
    discovery_method: str = Field(
        description="web_search / registry / llm_prior / portal_profiler"
    )
    known_type: DataSourceType | None = Field(
        default=None,
        description="Pre-determined type (for registry/portal results); None for web search results",
    )
    priority: str = "normal"  # high / normal / low
    metadata: dict = Field(default_factory=dict)


class SourceTypeClassification(BaseModel):
    """Single URL's type classification result."""

    url: str
    detected_types: list[DataSourceType]
    confidence: float = 0.0
    source: str = ""  # url_pattern / head_probe / llm


class TypeClassifiedCandidate(BaseModel):
    """A candidate after type classification (Stage 3.5 output)."""

    url: str
    title: str = ""
    snippet: str = ""
    source_engine: str = ""
    discovery_method: str = ""
    detected_types: list[DataSourceType]
    confidence: float = 0.0
    classification_source: str = ""  # url_pattern / head_probe / llm / pre_determined
    priority: str = "normal"
    metadata: dict = Field(default_factory=dict)


class ProcessedCandidate(BaseModel):
    """A candidate after type-specific processing (Stage 4 output)."""

    url: str
    title: str = ""
    source_type: DataSourceType
    is_alive: bool = True
    processing_details: dict = Field(default_factory=dict)
    # Type-specific data extracted during processing
    api_data: dict | None = None
    file_data: dict | None = None
    embedded_data: dict | None = None
    metadata: dict = Field(default_factory=dict)
