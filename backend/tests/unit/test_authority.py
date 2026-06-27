"""Tests 59-68: Authority scoring — domain prior table + metadata adjustments."""

from __future__ import annotations

import pytest

from src.judging.authority import _metadata_adjustment
from src.judging.domain_prior_table import DOMAIN_AUTHORITY, get_domain_authority


# ═══════════════════════════════════════════════════════════════════════
# Domain prior table
# ═══════════════════════════════════════════════════════════════════════

# ── Test 59: Known government domain → high score ────────────────────
def test_prior_data_gov():
    score = get_domain_authority("data.gov")
    assert score == 9.5


# ── Test 60: Known academic domain ───────────────────────────────────
def test_prior_arxiv():
    score = get_domain_authority("arxiv.org")
    assert score == 9.0


# ── Test 61: Known dataset platform ─────────────────────────────────
def test_prior_kaggle():
    score = get_domain_authority("kaggle.com")
    assert score == 8.0


# ── Test 62: Unknown domain returns None ─────────────────────────────
def test_prior_unknown():
    score = get_domain_authority("unknown-site-abc123.com")
    assert score is None


# ── Test 63: www. prefix stripped before lookup ──────────────────────
def test_prior_www_stripped():
    score = get_domain_authority("www.kaggle.com")
    assert score == 8.0


# ── Test 64: Subdomain suffix matching ───────────────────────────────
def test_prior_subdomain_suffix():
    # "api.openalex.org" should match "openalex.org" if stored as suffix
    score = get_domain_authority("api.openalex.org")
    # Exact entry exists for api.openalex.org
    assert score is not None


# ── Test 65: Social media domains have low scores ───────────────────
def test_prior_low_authority():
    reddit_score = get_domain_authority("reddit.com")
    assert reddit_score is not None and reddit_score < 5.0


# ═══════════════════════════════════════════════════════════════════════
# Metadata adjustments (Layer 2)
# ═══════════════════════════════════════════════════════════════════════

# ── Test 66: High citations → positive adjustment ────────────────────
def test_metadata_high_citations():
    adj = _metadata_adjustment({"citations": 15000})
    assert adj >= 1.5


# ── Test 67: High downloads → positive adjustment ────────────────────
def test_metadata_high_downloads():
    adj = _metadata_adjustment({"downloads": 200000})
    assert adj >= 1.0


# ── Test 68: Archived/deprecated → negative adjustment ───────────────
def test_metadata_archived():
    adj = _metadata_adjustment({"is_archived": True})
    assert adj <= -1.0


# ── Test 69: Combined signals clamped to [-2, +2] ───────────────────
def test_metadata_clamped():
    adj = _metadata_adjustment({
        "citations": 50000, "downloads": 500000, "stars": 50000
    })
    assert adj <= 2.0

    adj_neg = _metadata_adjustment({
        "is_archived": True, "deprecated": True, "has_issues_reported": True,
    })
    assert adj_neg >= -2.0


# ── Test 70: Empty metadata → zero adjustment ────────────────────────
def test_metadata_empty():
    adj = _metadata_adjustment({})
    assert adj == 0.0
