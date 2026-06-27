"""Tests 51-58: Extended license fit scoring edge cases."""

from src.judging.deterministic import score_license_fit


# ── Test 51: Open license (MIT) → 10.0 ──────────────────────────────
def test_license_mit():
    assert score_license_fit("MIT", user_constraint="commercial") == 10.0
    assert score_license_fit("MIT", user_constraint="open_only") == 10.0


# ── Test 52: Apache-2.0 → 10.0 ──────────────────────────────────────
def test_license_apache():
    assert score_license_fit("Apache-2.0", user_constraint="commercial") == 10.0


# ── Test 53: CC0 / public domain → 10.0 ─────────────────────────────
def test_license_cc0():
    assert score_license_fit("CC0-1.0", user_constraint="commercial") == 10.0
    assert score_license_fit("public domain", user_constraint="open_only") == 10.0


# ── Test 54: GPL (copyleft) → 7.0 for non-commercial, 5.0 for commercial
def test_license_gpl():
    assert score_license_fit("GPL-3.0", user_constraint="open_only") == 7.0
    assert score_license_fit("GPL-3.0", user_constraint="commercial") == 5.0


# ── Test 55: Proprietary + open_only → 0 (hard veto) ────────────────
def test_license_proprietary_open_only():
    assert score_license_fit("proprietary", user_constraint="open_only") == 0.0


# ── Test 56: Proprietary + commercial → 6.0 (OK if paying) ──────────
def test_license_proprietary_commercial():
    assert score_license_fit("proprietary license", user_constraint="commercial") == 6.0


# ── Test 57: Unknown license + commercial → 3.0 (risky) ─────────────
def test_license_unknown_commercial():
    assert score_license_fit(None, user_constraint="commercial") == 3.0


# ── Test 58: Unknown license + open_only → 5.0 (neutral) ────────────
def test_license_unknown_open_only():
    assert score_license_fit(None, user_constraint="open_only") == 5.0
