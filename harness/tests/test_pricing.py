"""DeepSeek cost recompute (runtime/pricing.py) — the fix for the SDK pricing
DeepSeek-routed tokens with Claude's table (~25-40x overstatement). Uses a real
run-log usage record as the ground-truth case."""

import os

import pytest

from runtime import pricing


@pytest.fixture(autouse=True)
def _deepseek_env(monkeypatch):
    # The harness always routes to DeepSeek; pricing.py keys off ANTHROPIC_BASE_URL
    # for the model-default path.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")


# A real ResultMessage.usage from the run logs (cache_read dominates).
_REAL_USAGE = {
    "input_tokens": 121480,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 4596480,
    "output_tokens": 45477,
}


def test_real_session_recompute_matches_deepseek_rates():
    got = pricing.cost_from_usage(_REAL_USAGE, "deepseek-v4-pro")
    expected = (121480 * 0.435 + 4596480 * 0.003625 + 45477 * 0.87) / 1e6
    assert got == pytest.approx(expected, rel=1e-9)
    # ≈ $0.109 — vs the SDK's $4.04 for this very session (~37x lower).
    assert 0.10 < got < 0.12


def test_flash_rates():
    got = pricing.cost_from_usage(_REAL_USAGE, "deepseek-v4-flash")
    expected = (121480 * 0.14 + 4596480 * 0.0028 + 45477 * 0.28) / 1e6
    assert got == pytest.approx(expected, rel=1e-9)


def test_prefixed_model_name_normalized():
    a = pricing.cost_from_usage(_REAL_USAGE, "deepseek/deepseek-v4-pro")
    b = pricing.cost_from_usage(_REAL_USAGE, "deepseek-v4-pro")
    assert a == b


def test_unknown_model_on_deepseek_endpoint_defaults_to_pro():
    # model None / unknown but routed to DeepSeek → priced at -pro (conservative).
    got = pricing.cost_from_usage(_REAL_USAGE, None)
    pro = pricing.cost_from_usage(_REAL_USAGE, "deepseek-v4-pro")
    assert got == pro


def test_non_deepseek_run_keeps_sdk_cost(monkeypatch):
    # No DeepSeek routing → cost_from_usage returns None, effective_cost keeps SDK.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    assert pricing.cost_from_usage(_REAL_USAGE, None) is None
    assert pricing.effective_cost(_REAL_USAGE, 4.04, None) == 4.04


def test_effective_cost_prefers_recompute_over_sdk():
    eff = pricing.effective_cost(_REAL_USAGE, 4.04, "deepseek-v4-pro")
    assert eff is not None and eff < 0.2  # recomputed, not the inflated SDK value


def test_empty_usage_returns_none():
    assert pricing.cost_from_usage({}, "deepseek-v4-pro") is None
    assert pricing.effective_cost(None, 1.23, "deepseek-v4-pro") == 1.23


def test_cache_creation_not_billed():
    # DeepSeek doesn't charge for cache writes — cache_creation must not add cost.
    u = {"input_tokens": 1000, "cache_creation_input_tokens": 999999, "output_tokens": 0}
    got = pricing.cost_from_usage(u, "deepseek-v4-pro")
    assert got == pytest.approx(1000 * 0.435 / 1e6, rel=1e-9)
