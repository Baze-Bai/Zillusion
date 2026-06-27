"""H7: a critic-LLM failure must not discard already-scored sources. The reflect
node falls back to force-sufficient so the pipeline proceeds to finalize with
the results in hand, instead of crashing the whole run."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.nodes import reflect as rf


def _src(score=7.0):
    return SimpleNamespace(
        source_type=SimpleNamespace(value="api"),
        name="Example",
        url="https://example.com/data",
        scores=SimpleNamespace(overall=score),
    )


def _state():
    return {
        "scored_sources": [_src(8.0), _src(6.0)],
        "requirement": SimpleNamespace(original_query="find x", domain="d"),
        "iteration": 0,  # < max_iterations, so the max-iter force-sufficient does NOT apply
        "max_iterations": 3,
    }


async def test_critic_llm_failure_force_sufficient_keeps_results():
    with patch.object(
        rf.llm_service, "complete_structured", AsyncMock(side_effect=TimeoutError("provider down"))
    ):
        out = await rf.reflect_node(_state())
    fb = out["critic_feedback"]
    # Force-sufficient ⇒ the reflect loop ENDS and finalize runs with the
    # scored sources already in state (they were never in this node's output —
    # the point is the run does NOT crash and does NOT loop).
    assert fb.is_sufficient is True
    assert out["iteration"] == 1
    # Cost from the failed call is zero, not a crash.
    assert "cost_accumulated" in out


async def test_critic_success_path_unaffected():
    from src.models.report import CriticOutput
    from src.services.llm import LLMUsage

    ok = CriticOutput(is_sufficient=False, gaps=["missing gov data"], next_round_feedback="add registries")
    with patch.object(
        rf.llm_service, "complete_structured", AsyncMock(return_value=(ok, LLMUsage()))
    ):
        out = await rf.reflect_node(_state())
    assert out["critic_feedback"].is_sufficient is False  # normal critic verdict respected
