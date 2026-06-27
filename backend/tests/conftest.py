"""Shared fixtures for backend test suite."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.candidates import (
    ProcessedCandidate,
    RawCandidate,
    TypeClassifiedCandidate,
)
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.models.report import CriticOutput, FinalReport
from src.models.requirement import StructuredRequirement
from src.models.scores import SourceScores
from src.models.specs import APISpec, EmbeddedSpec, FileSpec


# ---------------------------------------------------------------------------
# Requirement fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def finance_requirement() -> StructuredRequirement:
    return StructuredRequirement(
        original_query="Find stock market price data for US equities",
        domain="finance",
        sub_domains=["market", "equities"],
        data_type_hints=["timeseries", "api"],
        desired_formats=["json", "csv"],
        geographic_scope=["US"],
        temporal_range="2020-2024",
        update_frequency="daily",
        license_constraint="any",
        budget_constraint="free_only",
        search_keywords_en=["stock market data", "US equity prices", "financial data API"],
        search_keywords_zh=["美股数据", "股票行情"],
        sub_questions=[
            "Where can I find free US stock price history?",
            "Which APIs provide daily equity data?",
        ],
        known_authoritative_sources=["finance.yahoo.com", "polygon.io"],
        target_registries=["openalex", "kaggle"],
        target_schema={
            "properties": {
                "ticker": {},
                "date": {},
                "open": {},
                "close": {},
                "volume": {},
            }
        },
    )


@pytest.fixture
def health_requirement() -> StructuredRequirement:
    return StructuredRequirement(
        original_query="COVID-19 case data by country",
        domain="health",
        sub_domains=["epidemiology"],
        data_type_hints=["tabular", "timeseries"],
        desired_formats=["csv"],
        geographic_scope=["global"],
        license_constraint="open_only",
        budget_constraint="free_only",
        search_keywords_en=["COVID-19 cases data", "coronavirus statistics"],
        search_keywords_zh=["新冠疫情数据"],
        sub_questions=["Where can I find global COVID-19 case data?"],
        known_authoritative_sources=["data.who.int", "ourworldindata.org"],
    )


# ---------------------------------------------------------------------------
# Candidate fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def raw_candidate_web() -> RawCandidate:
    return RawCandidate(
        url="https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/day/2023-01-01/2024-01-01",
        title="Polygon.io Stock Data API",
        snippet="Real-time and historical stock market data",
        source_engine="exa",
        discovery_method="web_search",
    )


@pytest.fixture
def raw_candidate_registry() -> RawCandidate:
    return RawCandidate(
        url="https://huggingface.co/datasets/financial-data/us-stocks",
        title="US Stocks Dataset",
        snippet="Historical US stock prices",
        source_engine="huggingface",
        discovery_method="registry",
        known_type=DataSourceType.DOWNLOADABLE_FILE,
        priority="high",
    )


@pytest.fixture
def type_classified_api() -> TypeClassifiedCandidate:
    return TypeClassifiedCandidate(
        url="https://api.example.com/v1/data",
        title="Example API",
        snippet="Data API endpoint",
        source_engine="exa",
        discovery_method="web_search",
        detected_types=[DataSourceType.API],
        confidence=0.8,
        classification_source="url_pattern",
    )


@pytest.fixture
def type_classified_file() -> TypeClassifiedCandidate:
    return TypeClassifiedCandidate(
        url="https://data.gov/dataset.csv",
        title="Government Dataset",
        snippet="CSV data file",
        source_engine="brave",
        discovery_method="web_search",
        detected_types=[DataSourceType.DOWNLOADABLE_FILE],
        confidence=0.8,
        classification_source="url_pattern",
    )


@pytest.fixture
def processed_api() -> ProcessedCandidate:
    return ProcessedCandidate(
        url="https://api.example.com/v1/data",
        title="Example API",
        source_type=DataSourceType.API,
        is_alive=True,
        processing_details={"status_code": 200, "content_type": "application/json"},
        api_data={"endpoint": "https://api.example.com/v1/data", "method": "GET"},
        metadata={"domain": "finance", "discovery_method": "web_search"},
    )


@pytest.fixture
def processed_file() -> ProcessedCandidate:
    return ProcessedCandidate(
        url="https://data.gov/dataset.csv",
        title="Government Dataset",
        source_type=DataSourceType.DOWNLOADABLE_FILE,
        is_alive=True,
        processing_details={"status_code": 200, "content_type": "text/csv"},
        file_data={"download_url": "https://data.gov/dataset.csv", "file_format": "csv"},
        metadata={"domain": "government", "discovery_method": "web_search"},
    )


# ---------------------------------------------------------------------------
# DataSource fixtures
# ---------------------------------------------------------------------------

def make_datasource(
    *,
    url: str = "https://example.com/data",
    name: str = "Example Data",
    source_type: DataSourceType = DataSourceType.API,
    domain: str = "finance",
    access_level: AccessLevel = AccessLevel.OPEN,
    license: str | None = "MIT",
    scores: SourceScores | None = None,
    metadata: dict | None = None,
) -> DataSource:
    return DataSource(
        id="test-id-001",
        name=name,
        provider="example",
        url=url,
        source_type=source_type,
        description="Test data source",
        domain=domain,
        discovery_method="web_search",
        access_level=access_level,
        license=license,
        scores=scores,
        metadata=metadata or {},
    )


@pytest.fixture
def datasource_api() -> DataSource:
    return make_datasource(
        url="https://api.polygon.io/v2/aggs",
        name="Polygon Stock API",
        source_type=DataSourceType.API,
        domain="finance",
        access_level=AccessLevel.API_KEY_FREE,
        license="commercial license",
    )


@pytest.fixture
def datasource_file() -> DataSource:
    return make_datasource(
        url="https://data.gov/dataset.csv",
        name="Gov Dataset",
        source_type=DataSourceType.DOWNLOADABLE_FILE,
        domain="government",
        access_level=AccessLevel.OPEN,
        license="public domain",
    )


# ---------------------------------------------------------------------------
# Mock LLM service
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_service():
    """Mock the LLM service to avoid real API calls."""
    with patch("src.services.llm.llm_service") as mock:
        mock.complete = AsyncMock(return_value=('{"score": 7.0, "rationale": "test"}', MagicMock(cost_usd=0.001)))
        mock.complete_structured = AsyncMock()
        mock.embed = AsyncMock(return_value=[[0.1] * 128])
        mock.batch_complete = AsyncMock(return_value=[])
        yield mock


@pytest.fixture
def mock_probe():
    """Mock HTTP HEAD prober."""
    with patch("src.tools.validation.head_prober.probe_url") as mock:
        from src.tools.validation.head_prober import ProbeResult
        mock.return_value = ProbeResult(
            url="https://example.com",
            is_alive=True,
            status_code=200,
            content_type="application/json",
            content_length=1024,
        )
        yield mock
