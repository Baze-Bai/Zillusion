"""Tests 1-15: Pydantic model validation for all core models."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.models.candidates import (
    ProcessedCandidate,
    RawCandidate,
    SourceTypeClassification,
    TypeClassifiedCandidate,
)
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.models.report import CriticOutput, FinalReport
from src.models.requirement import StructuredRequirement
from src.models.routing import ActivatedWorkerSet
from src.models.scores import SourceScores
from src.models.specs import APISpec, EmbeddedSpec, FileSpec


# ── Test 1: SourceScores aggregate — normal weighted sum ─────────────
def test_scores_aggregate_normal():
    overall = SourceScores.aggregate(
        relevance=8.0, authority=7.0, freshness=6.0,
        accessibility=9.0, license_fit=8.0,
    )
    # 8*0.4 + 7*0.2 + 6*0.15 + 9*0.15 + 8*0.1 = 3.2+1.4+0.9+1.35+0.8 = 7.65
    assert 7.5 < overall < 7.8


# ── Test 2: SourceScores aggregate — license_fit=0 hard veto ────────
def test_scores_aggregate_license_veto():
    overall = SourceScores.aggregate(
        relevance=9.0, authority=9.0, freshness=9.0,
        accessibility=9.0, license_fit=0,
    )
    assert overall == -1


# ── Test 3: SourceScores aggregate — low relevance veto ─────────────
def test_scores_aggregate_relevance_veto():
    overall = SourceScores.aggregate(
        relevance=3.5, authority=8.0, freshness=8.0,
        accessibility=8.0, license_fit=8.0,
    )
    assert overall == -1


# ── Test 4: SourceScores aggregate — relevance exactly at threshold ──
def test_scores_aggregate_relevance_at_threshold():
    overall = SourceScores.aggregate(
        relevance=4.0, authority=5.0, freshness=5.0,
        accessibility=5.0, license_fit=5.0,
    )
    assert overall > 0  # Should NOT be vetoed


# ── Test 5: SourceScores field validation rejects out-of-range ───────
def test_scores_field_range_validation():
    with pytest.raises(ValidationError):
        SourceScores(
            relevance=11.0, authority=5.0, freshness=5.0,
            accessibility=5.0, license_fit=5.0, overall=5.0,
        )

    with pytest.raises(ValidationError):
        SourceScores(
            relevance=5.0, authority=-1.0, freshness=5.0,
            accessibility=5.0, license_fit=5.0, overall=5.0,
        )


# ── Test 6: DataSourceType enum values ──────────────────────────────
def test_datasource_type_enum():
    assert DataSourceType.API.value == "api"
    assert DataSourceType.DOWNLOADABLE_FILE.value == "file"
    assert DataSourceType.EMBEDDED_DATA.value == "embedded"


# ── Test 7: AccessLevel enum values ─────────────────────────────────
def test_access_level_enum():
    assert AccessLevel.OPEN.value == "open"
    assert AccessLevel.PAYWALL.value == "paywall"
    assert AccessLevel.UNKNOWN.value == "unknown"
    assert AccessLevel.API_KEY_FREE.value == "api_key_free"


# ── Test 8: DataSource model full construction ──────────────────────
def test_datasource_full_construction():
    ds = DataSource(
        id="abc123",
        name="Test Source",
        provider="TestProvider",
        url="https://example.com/api",
        source_type=DataSourceType.API,
        description="A test source",
        domain="finance",
        discovery_method="web_search",
    )
    assert ds.id == "abc123"
    assert ds.access_level == AccessLevel.UNKNOWN  # default
    assert ds.tags == []
    assert ds.scores is None
    assert isinstance(ds.discovered_at, datetime)


# ── Test 9: DataSource with all specs attached ──────────────────────
def test_datasource_with_specs():
    ds = DataSource(
        id="spec-test",
        name="Multi-spec",
        provider="test",
        url="https://example.com",
        source_type=DataSourceType.API,
        description="test",
        domain="tech",
        discovery_method="test",
        api_spec=APISpec(endpoint="https://example.com/api", method="POST", auth_type="api_key"),
        file_spec=FileSpec(download_url="https://example.com/data.csv", file_format="csv"),
    )
    assert ds.api_spec.method == "POST"
    assert ds.api_spec.auth_type == "api_key"
    assert ds.file_spec.file_format == "csv"


# ── Test 10: RawCandidate defaults ──────────────────────────────────
def test_raw_candidate_defaults():
    c = RawCandidate(
        url="https://example.com",
        source_engine="exa",
        discovery_method="web_search",
    )
    assert c.title == ""
    assert c.snippet == ""
    assert c.known_type is None
    assert c.priority == "normal"
    assert c.metadata == {}


# ── Test 11: TypeClassifiedCandidate multiple types ─────────────────
def test_type_classified_multiple_types():
    c = TypeClassifiedCandidate(
        url="https://github.com/owner/repo",
        detected_types=[DataSourceType.API, DataSourceType.DOWNLOADABLE_FILE],
        confidence=0.8,
        classification_source="url_pattern",
    )
    assert len(c.detected_types) == 2
    assert DataSourceType.API in c.detected_types


# ── Test 12: ProcessedCandidate dead URL ─────────────────────────────
def test_processed_candidate_dead_url():
    c = ProcessedCandidate(
        url="https://dead-link.example.com",
        source_type=DataSourceType.API,
        is_alive=False,
    )
    assert not c.is_alive
    assert c.api_data is None


# ── Test 13: StructuredRequirement defaults ─────────────────────────
def test_requirement_defaults():
    r = StructuredRequirement(original_query="find some data")
    assert r.license_constraint == "any"
    assert r.budget_constraint == "any"
    assert r.search_keywords_en == []
    assert r.target_schema is None


# ── Test 14: CriticOutput default is not sufficient ──────────────────
def test_critic_output_default():
    co = CriticOutput()
    assert co.is_sufficient is False
    assert co.gaps == []
    assert co.new_keywords is None


# ── Test 15: ActivatedWorkerSet default includes web ─────────────────
def test_activated_worker_set_default():
    aws = ActivatedWorkerSet()
    assert "web" in aws.workers
    assert aws.llm_reason == ""
