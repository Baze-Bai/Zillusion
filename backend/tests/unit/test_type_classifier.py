"""Tests 81-88: TypeClassifier 3-layer cascading classification."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.classifiers.type_classifier import classify_source_types
from src.models.data_source import DataSourceType
from src.tools.validation.head_prober import ProbeResult


# ── Test 81: Layer 1 — CSV file URL classified by regex ──────────────
@pytest.mark.asyncio
async def test_layer1_csv_url():
    results = await classify_source_types([
        ("https://data.gov/export/dataset.csv", "Government data export"),
    ])
    assert len(results) == 1
    assert DataSourceType.DOWNLOADABLE_FILE in results[0].detected_types
    assert results[0].source == "url_pattern"


# ── Test 82: Layer 1 — API version URL classified by regex ──────────
@pytest.mark.asyncio
async def test_layer1_api_version():
    results = await classify_source_types([
        ("https://api.example.com/v2/stocks", "Stock market API"),
    ])
    assert len(results) == 1
    assert DataSourceType.API in results[0].detected_types
    assert results[0].source == "url_pattern"


# ── Test 83: Layer 1 — Known domain kaggle.com → file ───────────────
@pytest.mark.asyncio
async def test_layer1_domain_map():
    results = await classify_source_types([
        ("https://kaggle.com/datasets/my-dataset", "ML dataset on Kaggle"),
    ])
    assert len(results) == 1
    assert DataSourceType.DOWNLOADABLE_FILE in results[0].detected_types


# ── Test 84: Layer 1 — github.com gets multiple types ───────────────
@pytest.mark.asyncio
async def test_layer1_github_multi_type():
    results = await classify_source_types([
        ("https://github.com/owner/repo", "Open source repository"),
    ])
    assert len(results) == 1
    types = results[0].detected_types
    assert DataSourceType.API in types or DataSourceType.DOWNLOADABLE_FILE in types


# ── Test 85: Layer 2 — HEAD probe detects CSV content-type ──────────
@pytest.mark.asyncio
async def test_layer2_head_probe_csv():
    with patch("src.classifiers.type_classifier.probe_url", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = ProbeResult(
            url="https://unknown.example.com/file",
            is_alive=True,
            status_code=200,
            content_type="text/csv",
        )
        results = await classify_source_types([
            ("https://unknown.example.com/file", "Some unknown file"),
        ])
        assert len(results) >= 1
        assert DataSourceType.DOWNLOADABLE_FILE in results[0].detected_types
        assert results[0].source == "head_probe"


# ── Test 86: Layer 2 — HEAD probe detects HTML → embedded ──────────
@pytest.mark.asyncio
async def test_layer2_head_probe_html():
    with patch("src.classifiers.type_classifier.probe_url", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = ProbeResult(
            url="https://unknown.example.com/page",
            is_alive=True,
            status_code=200,
            content_type="text/html; charset=utf-8",
        )
        results = await classify_source_types([
            ("https://unknown.example.com/page", "Some webpage"),
        ])
        assert len(results) >= 1
        assert DataSourceType.EMBEDDED_DATA in results[0].detected_types


# ── Test 87: Layer 3 — LLM fallback on dead probe → embedded fallback
@pytest.mark.asyncio
async def test_layer3_llm_fallback():
    with patch("src.classifiers.type_classifier.probe_url", new_callable=AsyncMock) as mock_probe, \
         patch("src.classifiers.type_classifier.llm_service") as mock_llm:
        mock_probe.return_value = ProbeResult(
            url="https://mystery.example.com",
            is_alive=False,
        )
        # LLM call fails → fallback to embedded
        mock_llm.complete = AsyncMock(side_effect=RuntimeError("LLM down"))

        results = await classify_source_types([
            ("https://mystery.example.com", "Totally unknown source"),
        ])
        assert len(results) >= 1
        assert DataSourceType.EMBEDDED_DATA in results[0].detected_types
        assert results[0].source == "fallback"
        assert results[0].confidence == 0.3


# ── Test 88: Empty input returns empty results ──────────────────────
@pytest.mark.asyncio
async def test_empty_input():
    results = await classify_source_types([])
    assert results == []
