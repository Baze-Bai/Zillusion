"""Phase-B api gating pieces in harness_orchestrator (no server, no LLM).

The bridge module is the real scripts/discovery_to_harness.py; its
HARNESS_INPUTS is patched onto a tmp dir so staging never touches the real
harness inputs/.
"""

from __future__ import annotations

import json

import pytest

from src.services import harness_orchestrator as orch

KEYED_API = {
    "id": "keyed01",
    "name": "Keyed Hotel API",
    "provider": "KeyedProvider",
    "url": "https://api.keyed.example/",
    "source_type": "api",
    "description": "hotel data, key required",
    "access_level": "api_key_free",
    "api_spec": {
        "endpoint": "https://api.keyed.example/v1/hotels",
        "auth_type": "api_key",
        "signup_url": "https://keyed.example/signup",
        "signup_instructions": "register, copy the key",
    },
    "metadata": {},
}

OPEN_API = {
    "id": "open01",
    "name": "Open Posts API",
    "provider": "OpenProvider",
    "url": "https://api.open.example/posts",
    "source_type": "api",
    "description": "free",
    "access_level": "open",
    "metadata": {},
}


@pytest.fixture
def patched_inputs(tmp_path, monkeypatch):
    bridge = orch._load_bridge()
    monkeypatch.setattr(bridge, "HARNESS_INPUTS", tmp_path)
    return tmp_path


def test_effective_max_iters_per_type_defaults():
    assert orch._effective_max_iters(None, "api") == 2
    assert orch._effective_max_iters(None, "extraction") == 5
    assert orch._effective_max_iters(None, "download") == 5
    assert orch._effective_max_iters(4, "api") == 4  # explicit wins


def test_keyed_api_gates_until_credentials(patched_inputs):
    bridge = orch._load_bridge()
    sid = bridge.stage_source(KEYED_API, "需要酒店数据", "api", patched_inputs)

    assert orch.api_site_ready(sid) is False
    info = orch._awaiting_credentials_info(sid)
    assert info["signup_url"] == "https://keyed.example/signup"
    assert info["write_to"] == f"inputs/{sid}/credentials.json"
    assert "api_key" in info["shape"]

    (patched_inputs / sid / "credentials.json").write_text(
        json.dumps({"api_key": "k-123456789"}), encoding="utf-8"
    )
    assert orch.api_site_ready(sid) is True


def test_open_api_is_ready_immediately(patched_inputs):
    bridge = orch._load_bridge()
    sid = bridge.stage_source(OPEN_API, "q", "api", patched_inputs)
    assert orch.api_site_ready(sid) is True
    # No awaiting marker was staged for it either.
    assert not (patched_inputs / sid / "credentials_needed.json").exists()


def test_awaiting_info_never_leaks_a_key(patched_inputs):
    bridge = orch._load_bridge()
    sid = bridge.stage_source(KEYED_API, "q", "api", patched_inputs)
    (patched_inputs / sid / "credentials.json").write_text(
        json.dumps({"api_key": "sk-super-secret-987654321"}), encoding="utf-8"
    )
    info = orch._awaiting_credentials_info(sid)
    assert "sk-super-secret-987654321" not in json.dumps(info)


# ── CredentialsGate fallback ladder fields (C1) ──────────────────────


def test_awaiting_info_tier1_has_signup_plus_provider(patched_inputs):
    bridge = orch._load_bridge()
    sid = bridge.stage_source(KEYED_API, "q", "api", patched_inputs)
    info = orch._awaiting_credentials_info(sid)
    # Tier 1: precise signup_url present, AND provider/source_url for the card.
    assert info["signup_url"] == "https://keyed.example/signup"
    assert info["provider"] == "KeyedProvider"
    assert info["source_url"] == "https://api.keyed.example/"


def test_awaiting_info_tier2_provider_auth_docs_no_signup(patched_inputs):
    bridge = orch._load_bridge()
    src = dict(KEYED_API)
    # No signup_url, but auth + a docs URL distinct from the homepage → tier 2.
    src["api_spec"] = dict(KEYED_API["api_spec"], signup_url=None,
                           documentation_url="https://docs.keyed.example/")
    sid = bridge.stage_source(src, "q", "api", patched_inputs)
    info = orch._awaiting_credentials_info(sid)
    assert not info.get("signup_url")
    assert info["provider"] == "KeyedProvider"
    assert info["source_url"] == "https://api.keyed.example/"
    assert info["auth_type"] == "api_key"
    assert info["docs_url"] == "https://docs.keyed.example/"


def test_awaiting_info_tier3_drops_docs_equal_to_homepage(patched_inputs):
    bridge = orch._load_bridge()
    src = dict(KEYED_API)
    # docs == homepage → not surfaced (no real next hop); tier 3 floor.
    src["api_spec"] = dict(KEYED_API["api_spec"], signup_url=None,
                           documentation_url="https://api.keyed.example/")
    sid = bridge.stage_source(src, "q", "api", patched_inputs)
    info = orch._awaiting_credentials_info(sid)
    assert "docs_url" not in info
    assert info["provider"] == "KeyedProvider"
    assert info["source_url"] == "https://api.keyed.example/"
