"""Part A/B: signup data-flow fix + keyed-signup soft-warn + field provenance.

Pure-logic tests (no session IO): _build_data_sources' APISpec construction
(A1), the keyed-API-missing-signup predicate behind commit_source's soft warn
(A3), and the server-side field_notes stamper (B2).
"""

from __future__ import annotations

from src.agents.agentic.runner import _build_data_sources
from src.agents.agentic.tools import _keyed_api_missing_signup, _stamp_field_notes


# ── A1: _build_data_sources reads the FULL api spec ──────────────────


def test_build_data_sources_reads_full_api_spec():
    src = {
        "url": "https://api.x.com",
        "name": "X API",
        "source_type": "api",
        "description": "the X api",
        "access_level": "api_key_free",
        "api_endpoint": "https://api.x.com/v1/items",
        "api_method": "GET",
        "auth_type": "api_key",
        "auth_location": "header",
        "auth_param_name": "X-Api-Key",
        "signup_url": "https://x.com/signup",
        "signup_instructions": "register then copy the key",
        "docs_url": "https://docs.x.com",
        "openapi_spec_url": "https://x.com/openapi.json",
        "has_sdk": True,
    }
    out = _build_data_sources(src)
    assert len(out) == 1
    spec = out[0].api_spec
    assert spec is not None
    # The fields that USED to be silently dropped now survive.
    assert spec.signup_url == "https://x.com/signup"
    assert spec.signup_instructions == "register then copy the key"
    assert spec.auth_location == "header"
    assert spec.auth_param_name == "X-Api-Key"
    assert spec.openapi_spec_url == "https://x.com/openapi.json"
    assert spec.has_sdk is True
    assert spec.endpoint == "https://api.x.com/v1/items"
    assert spec.documentation_url == "https://docs.x.com"


def test_build_data_sources_api_omitted_fields_default():
    # Agent omits the optional api fields → None/default; zero regression.
    src = {"url": "https://api.y.com", "name": "Y", "source_type": "api", "description": "d"}
    spec = _build_data_sources(src)[0].api_spec
    assert spec.signup_url is None
    assert spec.has_sdk is False
    assert spec.endpoint == "https://api.y.com"  # falls back to url


# ── A3: keyed-API-missing-signup predicate (drives the soft warn) ────


def test_keyed_missing_signup_true():
    assert _keyed_api_missing_signup({"source_type": "api", "access_level": "api_key_free"}) is True


def test_keyed_with_signup_top_level_false():
    src = {"source_type": "api", "access_level": "api_key_free", "signup_url": "https://s"}
    assert _keyed_api_missing_signup(src) is False


def test_keyed_with_signup_in_metadata_false():
    src = {"source_type": "api", "access_level": "oauth", "metadata": {"signup_url": "https://s"}}
    assert _keyed_api_missing_signup(src) is False


def test_open_api_not_flagged():
    assert _keyed_api_missing_signup({"source_type": "api", "access_level": "open"}) is False


def test_non_api_not_flagged():
    src = {"source_type": "embedded", "access_level": "api_key_free"}
    assert _keyed_api_missing_signup(src) is False


def test_unknown_access_level_is_keyed():
    # (b) "all" — unknown is conservatively treated as needing a key.
    assert _keyed_api_missing_signup({"source_type": "api", "access_level": "unknown"}) is True


def test_keyed_multicategory_list():
    src = {"source_type": ["embedded", "api"], "access_level": "api_key_paid"}
    assert _keyed_api_missing_signup(src) is True


# ── B2: server-side field_notes stamper ──────────────────────────────


def test_stamp_fill_marks_applied_and_timestamps():
    rec = {"metadata": {"field_notes": [
        {"field": "signup_url", "action": "fill", "value": "https://s", "source": "search_web: x"}
    ]}}
    _stamp_field_notes(rec)
    n = rec["metadata"]["field_notes"][0]
    assert n["status"] == "applied"
    assert n["at"]  # server-stamped, non-empty


def test_stamp_propose_marks_pending_review():
    rec = {"metadata": {"field_notes": [
        {"field": "auth_type", "action": "propose", "original": "unknown",
         "value": "api_key", "reason": "signup page says register for a key", "source": "u"}
    ]}}
    _stamp_field_notes(rec)
    assert rec["metadata"]["field_notes"][0]["status"] == "pending_review"


def test_stamp_drops_propose_without_reason():
    rec = {"metadata": {"field_notes": [{"field": "x", "action": "propose", "value": "v"}]}}
    _stamp_field_notes(rec)
    assert rec["metadata"]["field_notes"] == []


def test_stamp_drops_bad_action_and_fieldless():
    rec = {"metadata": {"field_notes": [
        {"field": "x", "action": "delete", "value": "v"},   # bad action
        {"action": "fill", "value": "v"},                    # no field
        {"field": "ok", "action": "fill", "value": "v", "source": "s"},  # keep
    ]}}
    _stamp_field_notes(rec)
    kept = rec["metadata"]["field_notes"]
    assert len(kept) == 1 and kept[0]["field"] == "ok"


def test_stamp_no_metadata_is_noop():
    rec = {"url": "x"}
    _stamp_field_notes(rec)  # must not raise
    assert "metadata" not in rec


def test_stamp_field_notes_not_a_list_is_noop():
    rec = {"metadata": {"field_notes": "oops"}}
    _stamp_field_notes(rec)  # must not raise
    assert rec["metadata"]["field_notes"] == "oops"
