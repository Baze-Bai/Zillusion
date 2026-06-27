"""H6: a crashed discovery agent (AgentRunResult.error set) must surface as a
failure, not flow into judge/finalize as a clean 0-sources success."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.agents.agentic.runner import AgentRunResult
from src.agents.nodes import agentic_discovery as ad


def _result(error=None, sources=None):
    return AgentRunResult(
        sources=sources or [],
        cost_usd=0.1,
        duration_ms=10.0,
        num_turns=3,
        session_id="s",
        raw_final_text="",
        error=error,
    )


def _state():
    # requirement just needs to be non-None with the attrs the node reads.
    req = type("Req", (), {"original_query": "find x", "domain": "d"})()
    return {"requirement": req, "iteration": 0}


async def test_crash_result_raises():
    with patch.object(ad, "_run", return_value=_result(error="RuntimeError: boom")):
        with pytest.raises(RuntimeError, match="discovery agent failed"):
            await ad.agentic_discovery_node(_state())


async def test_genuine_empty_does_not_raise():
    # error=None with 0 sources is a legitimate (if empty) result — must NOT raise.
    with patch.object(ad, "_run", return_value=_result(error=None, sources=[])):
        out = await ad.agentic_discovery_node(_state())
    assert out["normalized_sources"] == [] and out["deduplicated_sources"] == []


async def test_run_none_fallback_carries_error():
    # The stream-ended-with-no-result fallback must flag error so the node raises.
    async def _empty_stream(*a, **k):
        return
        yield  # noqa: unreachable — makes this an async generator

    with patch.object(ad, "stream_discovery_agent", _empty_stream):
        result = await ad._run(_state()["requirement"], 0)
    assert result.error is not None
