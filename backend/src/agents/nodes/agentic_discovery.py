"""LangGraph node: agentic super-node replacing 9 deterministic Stage-2/3/4/5 nodes.

Position in the graph (set in src.agents.graph):

    parse_intent → agentic_discovery → judge → reflect → diagnostics_writeback → finalize

This node owns:
  - route_sources (Stage 2)
  - discover (Stage 3)
  - coarse_filter (Stage 3A)
  - portal_detect (Stage 3B)
  - expand_portals (Stage 3D)
  - content_classify (Stage 3C)
  - merge_urls (Stage 3E)
  - process_by_type (Stage 4)
  - normalize_dedupe (Stage 5)

The agent uses the 10 tools in src.agents.agentic.tools and follows the
system prompt in src.agents.agentic.system_prompt. Output is fed straight
into judge as ``scored_sources`` (judge will then score and Stage 7+
continue unchanged).

Reflect-loop semantics: when reflect sends us back, ``iteration`` increments
and the agent runs again. The agent prompt sees the previous critic_feedback
so it knows what gaps to close.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from langchain_core.callbacks.manager import adispatch_custom_event

from src.agents.agentic.runner import stream_discovery_agent, AgentRunResult
from src.models.state import AgentState
from src.services.run_logger import log_event
from src.services.run_registry import get_steer_queue

logger = logging.getLogger(__name__)


async def agentic_discovery_node(state: AgentState) -> dict:
    """Run the Claude Agent SDK discovery loop. Emits scored_sources (pre-judge shape)."""
    start = time.monotonic()
    requirement = state.get("requirement")
    if requirement is None:
        logger.warning("agentic_discovery: no requirement in state, skipping")
        return {
            "deduplicated_sources": [],
            "normalized_sources": [],
            "stage_timings": {**state.get("stage_timings", {}), "agentic_discovery": 0},
        }

    # Carry critic feedback into prompt on loop iterations
    iteration = state.get("iteration", 0)
    critic = state.get("critic_feedback")
    if iteration > 0 and critic is not None:
        # Reflect-loop: tack the critic's feedback onto the requirement for context.
        # The agent will see it via original_query enrichment.
        feedback_str = (
            f"\n\n[Reflect-loop iteration {iteration}] Previous critic feedback:\n"
            f"  is_sufficient={critic.is_sufficient}\n"
            f"  gaps={critic.gaps}\n"
            f"  next_round_feedback={critic.next_round_feedback}\n"
            f"Focus on closing those gaps."
        )
        log_event("agentic_discovery.prompt_enrichment", {
            "iteration": iteration,
            "critic_is_sufficient": critic.is_sufficient,
            "critic_gaps": list(critic.gaps or []),
            "critic_next_round_feedback": (critic.next_round_feedback or "")[:800],
            "enrichment_text": feedback_str,
        })
        # Don't mutate the requirement object; pass via a side channel — for
        # MVP we mutate original_query, then restore. (Future: extend the
        # requirement shape with a 'critic_feedback' field.)
        original = requirement.original_query
        requirement.original_query = original + feedback_str
        try:
            result = await _run(requirement, iteration)
        finally:
            requirement.original_query = original
    else:
        result = await _run(requirement, iteration)

    duration = time.monotonic() - start

    # A crashed discovery run flags `error` and returns []. Surface it as a real
    # failure instead of letting [] flow into judge/finalize as a clean
    # "0 sources" success (which advances cost, fires no retry/alert, and hands
    # the user an empty report for what was actually an infrastructure crash).
    if result.error:
        log_event("agentic_discovery.failed", {
            "iteration": iteration, "error": result.error,
            "cost_usd": result.cost_usd, "duration_ms": round(duration * 1000, 1),
        })
        raise RuntimeError(f"discovery agent failed: {result.error}")

    logger.info(
        "agentic_discovery complete: %d sources, cost=$%.4f, turns=%d, %.1fs (iter %d)",
        len(result.sources), result.cost_usd, result.num_turns, duration, iteration,
    )

    # Emit one source_committed event per discovered source so the UI can
    # render each site as its own box with FULL data. The live tool stream
    # truncates tool inputs to 300 chars, so we surface the complete objects
    # here at node end. The frontend reducer upserts by source id, so the
    # reflect loop re-emitting the same source across iterations is idempotent.
    for src in result.sources:
        try:
            payload = src.model_dump(mode="json") if hasattr(src, "model_dump") else src
            await adispatch_custom_event("source_committed", {"source": payload})
        except Exception as e:
            logger.debug("dispatch source_committed failed: %s", e)

    # Feed sources directly into the post-stages. We populate both
    # normalized_sources and deduplicated_sources so the existing judge /
    # finalize that read either field stay functional.
    return {
        "normalized_sources": result.sources,
        "deduplicated_sources": result.sources,
        # Portal trees produced by the agent — surfaced for finalize to
        # include in FinalReport if it chooses to. Empty list when the
        # agent expanded no portals this run.
        "portal_trees": result.portal_trees,
        "stage_timings": {
            **state.get("stage_timings", {}),
            "agentic_discovery": duration,
        },
        "cost_accumulated": state.get("cost_accumulated", 0.0) + result.cost_usd,
    }


async def _run(requirement, iteration: int) -> AgentRunResult:
    """Streaming wrapper that drains events while keeping the final result."""
    result: AgentRunResult | None = None
    event_count = 0
    tool_calls = 0

    # workspace_dir defaults to the query's session dir
    # (`agent-workspace/agent-sessions/<query_id>/`) inside stream_discovery_agent,
    # so the agent's cwd aligns with the emit-as-you-go JSONL + fetched/ files.
    # Per-iteration isolation is no longer needed — the session dir is stable
    # across reflect-loop iterations and audit benefits from that stability.
    # Turn-level live steering: when the run is driven by run_executor, the
    # RunHandle's input queue is exposed via this ContextVar. None for callers
    # outside a steerable run (tests, batch) → unchanged one-shot behavior.
    steer_queue = get_steer_queue()
    async for item in stream_discovery_agent(
        requirement,
        max_turns=100,
        steer_queue=steer_queue,
    ):
        if isinstance(item, AgentRunResult):
            result = item
        else:
            event_count += 1
            if item.kind == "tool_started":
                tool_calls += 1
            elif item.kind == "error":
                logger.warning("agentic_discovery agent error event: %s", item.data)
            elif item.kind == "task_description_committed":
                # Forward to the LangGraph stream so the SSE route can
                # surface task_description.md to the user the moment the
                # agent writes it — early-warning channel for "did the
                # agent understand the query right?".
                try:
                    await adispatch_custom_event(
                        "task_description_committed",
                        item.data,
                    )
                except Exception as e:
                    logger.debug("dispatch task_description event failed: %s", e)
            elif item.kind == "agent_text":
                # Forward the agent's running narration so the UI renders a
                # live chat (not just stage markers). Best-effort.
                try:
                    await adispatch_custom_event("agent_text", item.data)
                except Exception as e:
                    logger.debug("dispatch agent_text event failed: %s", e)
            elif item.kind == "agent_message":
                # The agent's deliberate message TO the user (send_user_message
                # tool) — forwarded as a custom event so run_executor persists +
                # publishes it as a left-aligned chat bubble. Best-effort.
                try:
                    await adispatch_custom_event("agent_message", item.data)
                except Exception as e:
                    logger.debug("dispatch agent_message event failed: %s", e)

    logger.debug(
        "agentic_discovery streamed %d events, %d tool_calls",
        event_count, tool_calls,
    )
    if result is None:
        from src.agents.agentic.runner import AgentRunResult as _ARR
        result = _ARR(
            sources=[], cost_usd=0.0, duration_ms=0.0,
            num_turns=0, session_id=None, raw_final_text="",
            error="discovery agent stream ended with no result",
        )
    return result
