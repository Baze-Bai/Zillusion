"""Tests 26-35: URL pattern matching and type classification layer 1."""

import pytest

from src.classifiers.url_patterns import (
    API_PATTERNS,
    DOMAIN_TYPE_MAP,
    FILE_PATTERNS,
    KNOWN_PORTALS,
    PORTAL_URL_PATTERNS,
)


# ── Test 26: CSV file URL detected ──────────────────────────────────
def test_file_pattern_csv():
    url = "https://example.com/data/export.csv"
    assert any(p.search(url) for p in FILE_PATTERNS)


# ── Test 27: JSON file URL detected ─────────────────────────────────
def test_file_pattern_json():
    url = "https://example.com/datasets/records.json"
    assert any(p.search(url) for p in FILE_PATTERNS)


# ── Test 28: Parquet file URL detected ───────────────────────────────
def test_file_pattern_parquet():
    url = "https://storage.example.com/data.parquet"
    assert any(p.search(url) for p in FILE_PATTERNS)


# ── Test 29: /download/ path detected ───────────────────────────────
def test_file_pattern_download_path():
    url = "https://example.com/download/my-dataset"
    assert any(p.search(url) for p in FILE_PATTERNS)


# ── Test 30: API version path detected ──────────────────────────────
def test_api_pattern_version():
    url = "https://example.com/v2/stocks/AAPL"
    assert any(p.search(url) for p in API_PATTERNS)


# ── Test 31: /api/ path detected ────────────────────────────────────
def test_api_pattern_api_path():
    url = "https://example.com/api/users/123"
    assert any(p.search(url) for p in API_PATTERNS)


# ── Test 32: /graphql endpoint detected ──────────────────────────────
def test_api_pattern_graphql():
    url = "https://example.com/graphql"
    assert any(p.search(url) for p in API_PATTERNS)


# ── Test 33: Known domain → type mapping ─────────────────────────────
def test_domain_type_map_kaggle():
    assert "file" in DOMAIN_TYPE_MAP["kaggle.com"]


def test_domain_type_map_github_multi():
    assert "api" in DOMAIN_TYPE_MAP["github.com"]
    assert "file" in DOMAIN_TYPE_MAP["github.com"]


# ── Test 34: Portal URL patterns ─────────────────────────────────────
def test_portal_url_data_prefix():
    url = "https://data.gov.uk/datasets"
    assert any(p.search(url) for p in PORTAL_URL_PATTERNS)


def test_portal_url_opendata_prefix():
    url = "https://opendata.cityofnewyork.us"
    assert any(p.search(url) for p in PORTAL_URL_PATTERNS)


# ── Test 35: Known portals whitelist ─────────────────────────────────
def test_known_portals_contains_key_sites():
    assert "data.gov" in KNOWN_PORTALS
    assert "kaggle.com" in KNOWN_PORTALS
    assert "data.worldbank.org" in KNOWN_PORTALS
    assert "fred.stlouisfed.org" in KNOWN_PORTALS


def test_known_portals_contains_intl_sites():
    assert "datos.gob.es" in KNOWN_PORTALS
    assert "data.go.jp" in KNOWN_PORTALS
    assert "data.gov.cn" in KNOWN_PORTALS
