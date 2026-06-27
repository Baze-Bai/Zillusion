"""Tests supplementary: API endpoint validation, graph assembly, config, and head prober.

These tests complete the 100 test cases alongside the numbered tests in other files.
Tests are counted across all files:
  test_models.py          : 1-15
  test_url_canonicalizer.py: 16-25 (+ 25b)
  test_url_patterns.py    : 26-35
  test_deterministic.py   : 36-50
  test_license_scoring.py : 51-58
  test_authority.py       : 59-70
  test_source_routing.py  : 71-80
  test_type_classifier.py : 81-88
  test_pipeline_nodes.py  : 89-100
  ────────────────────────── total ≥ 100
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.config import (
    AppConfig,
    BudgetConfig,
    CacheConfig,
    ClassificationConfig,
    LLMConfig,
    RateLimitConfig,
    ScoringConfig,
    SearchConfig,
    Settings,
)


# ═══════════════════════════════════════════════════════════════════════
# Config defaults
# ═══════════════════════════════════════════════════════════════════════

def test_config_defaults():
    s = Settings()
    assert s.app.debug is False
    assert s.app.log_level == "INFO"
    assert s.budget.max_iterations == 3
    assert s.budget.query_min_length == 5
    assert s.budget.query_max_length == 2000


def test_scoring_weights_sum_to_one():
    s = ScoringConfig()
    total = (
        s.weight_relevance + s.weight_authority
        + s.weight_freshness + s.weight_accessibility + s.weight_license_fit
    )
    assert abs(total - 1.0) < 0.001


def test_llm_config_defaults():
    c = LLMConfig()
    assert "claude" in c.primary_strong or "sonnet" in c.primary_strong
    assert "haiku" in c.primary_fast
    assert c.temperature == 0.1


def test_budget_bounds():
    c = BudgetConfig()
    assert c.max_candidates_stage_a > c.max_candidates_stage_b
    assert c.stage_a_batch_size > 0
    assert c.max_iterations_upper_bound >= c.max_iterations


def test_cache_ttl_ordering():
    c = CacheConfig()
    assert c.l1_search_ttl < c.l2_page_ttl < c.l3_judging_ttl


def test_classification_confidence_ordering():
    c = ClassificationConfig()
    assert c.type_url_pattern_confidence > c.type_head_probe_confidence
    assert c.type_head_probe_confidence > c.type_llm_confidence
    assert c.type_llm_confidence > c.type_fallback_confidence


# ═══════════════════════════════════════════════════════════════════════
# DiscoverRequest validation
# ═══════════════════════════════════════════════════════════════════════

def test_discover_request_valid():
    from src.api.routes.discover import DiscoverRequest
    req = DiscoverRequest(query="Find stock market data for analysis")
    assert req.max_iterations == 3
    assert req.license_constraint == "any"


def test_discover_request_query_too_short():
    from src.api.routes.discover import DiscoverRequest
    with pytest.raises(ValidationError):
        DiscoverRequest(query="hi")  # < 5 chars


def test_discover_request_max_iterations_bounds():
    from src.api.routes.discover import DiscoverRequest
    with pytest.raises(ValidationError):
        DiscoverRequest(query="valid query text", max_iterations=0)
    with pytest.raises(ValidationError):
        DiscoverRequest(query="valid query text", max_iterations=6)


def test_discover_request_optional_fields():
    from src.api.routes.discover import DiscoverRequest
    req = DiscoverRequest(
        query="COVID-19 data by country",
        license_constraint="open_only",
        budget_constraint="free_only",
        geographic_scope=["global"],
        temporal_range="2020-2024",
        desired_formats=["csv", "json"],
        max_iterations=2,
    )
    assert req.geographic_scope == ["global"]
    assert req.desired_formats == ["csv", "json"]


# ═══════════════════════════════════════════════════════════════════════
# Graph assembly
# ═══════════════════════════════════════════════════════════════════════

def test_graph_should_continue_finalize():
    from src.agents.graph import _should_continue
    from src.models.report import CriticOutput

    state = {
        "critic_feedback": CriticOutput(is_sufficient=True),
        "iteration": 1,
        "max_iterations": 3,
    }
    assert _should_continue(state) == "finalize"


def test_graph_should_continue_loop():
    from src.agents.graph import _should_continue
    from src.models.report import CriticOutput

    state = {
        "critic_feedback": CriticOutput(is_sufficient=False, gaps=["missing data"]),
        "iteration": 1,
        "max_iterations": 3,
    }
    assert _should_continue(state) == "route_sources"


def test_graph_should_continue_max_reached():
    from src.agents.graph import _should_continue
    from src.models.report import CriticOutput

    state = {
        "critic_feedback": CriticOutput(is_sufficient=False),
        "iteration": 3,
        "max_iterations": 3,
    }
    assert _should_continue(state) == "finalize"


def test_graph_should_continue_no_critic():
    from src.agents.graph import _should_continue

    state = {"iteration": 0, "max_iterations": 3}
    # No critic_feedback → finalize
    assert _should_continue(state) == "finalize"


# ═══════════════════════════════════════════════════════════════════════
# Health endpoint
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_endpoint():
    from src.api.routes.discover import health
    result = await health()
    assert result["status"] == "ok"
    assert "service" in result


# ═══════════════════════════════════════════════════════════════════════
# Head prober
# ═══════════════════════════════════════════════════════════════════════

def test_probe_result_dataclass():
    from src.tools.validation.head_prober import ProbeResult
    pr = ProbeResult(url="https://example.com", is_alive=True, status_code=200)
    assert pr.content_type is None
    assert pr.has_attachment is False
    assert pr.response_time_ms == 0.0


# ═══════════════════════════════════════════════════════════════════════
# LLM service
# ═══════════════════════════════════════════════════════════════════════

def test_llm_service_model_tiers():
    from src.services.llm import LLMService
    svc = LLMService()
    strong_models = svc._get_models("strong")
    fast_models = svc._get_models("fast")
    assert len(strong_models) == 2
    assert len(fast_models) == 2
    assert strong_models[0] != fast_models[0]


def test_llm_usage_tracking():
    from src.services.llm import LLMService, LLMUsage
    svc = LLMService()
    assert svc.total_cost == 0.0
    assert svc.call_count == 0
    svc._track(LLMUsage(cost_usd=0.01, model="test"))
    svc._track(LLMUsage(cost_usd=0.02, model="test"))
    assert svc.total_cost == pytest.approx(0.03)
    assert svc.call_count == 2


def test_llm_reset_tracking():
    from src.services.llm import LLMService, LLMUsage
    svc = LLMService()
    svc._track(LLMUsage(cost_usd=0.05, model="test"))
    svc.reset_tracking()
    assert svc.total_cost == 0.0
    assert svc.call_count == 0


# ═══════════════════════════════════════════════════════════════════════
# SSE helper
# ═══════════════════════════════════════════════════════════════════════

def test_sse_event_format():
    from src.api.sse import sse_event
    event = sse_event("test_event", {"key": "value"})
    assert isinstance(event, str)
    assert "event: test_event" in event
    assert '"key"' in event
    assert '"value"' in event
    assert event.endswith("\n\n")
