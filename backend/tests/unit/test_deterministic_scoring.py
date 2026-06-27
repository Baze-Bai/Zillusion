"""Tests 36-50: Deterministic scoring — freshness, accessibility, license fit."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.judging.deterministic import score_accessibility, score_freshness, score_license_fit
from src.models.data_source import AccessLevel


# ═══════════════════════════════════════════════════════════════════════
# Freshness scoring
# ═══════════════════════════════════════════════════════════════════════

# ── Test 36: Updated today → score close to 10 ──────────────────────
def test_freshness_today():
    score = score_freshness(datetime.utcnow(), domain="finance")
    assert score >= 9.5


# ── Test 37: Updated one half-life ago → score ~5 ───────────────────
def test_freshness_one_halflife():
    # Finance half-life = 30 days
    one_halflife_ago = datetime.utcnow() - timedelta(days=30)
    score = score_freshness(one_halflife_ago, domain="finance")
    assert 4.5 <= score <= 5.5


# ── Test 38: Updated two half-lives ago → score ~2.5 ────────────────
def test_freshness_two_halflives():
    two_halflives_ago = datetime.utcnow() - timedelta(days=60)
    score = score_freshness(two_halflives_ago, domain="finance")
    assert 2.0 <= score <= 3.0


# ── Test 39: None date → neutral score (5.0) ────────────────────────
def test_freshness_none_date():
    score = score_freshness(None, domain="finance")
    assert score == 5.0


# ── Test 40: Invalid date string → neutral score ────────────────────
def test_freshness_invalid_string():
    score = score_freshness("not-a-date", domain="finance")
    assert score == 5.0


# ── Test 41: ISO date string parsed correctly ────────────────────────
def test_freshness_iso_string():
    today_iso = datetime.utcnow().isoformat()
    score = score_freshness(today_iso, domain="finance")
    assert score >= 9.0


# ── Test 42: Domain-specific half-life affects decay rate ────────────
def test_freshness_domain_specific():
    one_month_ago = datetime.utcnow() - timedelta(days=30)
    score_news = score_freshness(one_month_ago, domain="news")      # half-life=7 → old
    score_science = score_freshness(one_month_ago, domain="science")  # half-life=365 → fresh
    assert score_science > score_news


# ── Test 43: Unknown domain falls back to default half-life ──────────
def test_freshness_unknown_domain():
    one_month_ago = datetime.utcnow() - timedelta(days=30)
    score_unknown = score_freshness(one_month_ago, domain="unicorn")
    score_default = score_freshness(one_month_ago, domain="default")
    assert score_unknown == score_default


# ═══════════════════════════════════════════════════════════════════════
# Accessibility scoring
# ═══════════════════════════════════════════════════════════════════════

# ── Test 44: OPEN → 10.0 ────────────────────────────────────────────
def test_accessibility_open():
    assert score_accessibility(AccessLevel.OPEN) == 10.0


# ── Test 45: PAYWALL → 2.0 ──────────────────────────────────────────
def test_accessibility_paywall():
    assert score_accessibility(AccessLevel.PAYWALL) == 2.0


# ── Test 46: UNKNOWN → 3.0 ──────────────────────────────────────────
def test_accessibility_unknown():
    assert score_accessibility(AccessLevel.UNKNOWN) == 3.0


# ── Test 47: String enum value works ─────────────────────────────────
def test_accessibility_string_input():
    assert score_accessibility("api_key_free") == 7.0


# ── Test 48: Unrecognized string falls back to unknown ───────────────
def test_accessibility_unrecognized():
    assert score_accessibility("some_random") == 3.0


# ═══════════════════════════════════════════════════════════════════════
# License fit scoring
# ═══════════════════════════════════════════════════════════════════════

# ── Test 49: Constraint "any" → 8.0 (no constraint) ─────────────────
def test_license_any():
    assert score_license_fit("MIT", user_constraint="any") == 8.0
    assert score_license_fit(None, user_constraint="any") == 8.0


# ── Test 50: Non-commercial license + commercial need → 0 (hard veto)
def test_license_noncommercial_veto():
    assert score_license_fit("CC-BY-NC-4.0", user_constraint="commercial") == 0.0
    assert score_license_fit("non-commercial license", user_constraint="commercial") == 0.0
    assert score_license_fit("academic use only", user_constraint="commercial") == 0.0
