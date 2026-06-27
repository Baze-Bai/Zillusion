"""Main LangGraph state machine — agentic hybrid edition.

Stage-3/4/5 redesign (2026-05-11):
  Replaced the 9-node deterministic middle section
    (route_sources / discover / coarse_filter / portal_detect /
     expand_portals / content_classify / merge_urls / process_by_type /
     normalize_dedupe)
  with a SINGLE agentic super-node `agentic_discovery` driven by
  Claude Agent SDK + DeepSeek's Anthropic-compatible endpoint. The agent
  loops over the 10 tools defined in src.agents.agentic.tools.

Surviving deterministic nodes (kept for their well-tuned prompts / logic):
  - parse_intent       (Stage 1)
  - judge              (Stage 6)  — 5-dim scoring + veto rules
  - reflect            (Stage 7)  — critic + loop decision
  - finalize           (Stage 8)  — assembles FinalReport

Graph flow:

  parse_intent → agentic_discovery → judge → reflect →
    CONDITIONAL:
      if !sufficient AND iteration < max → agentic_discovery (loop back)
      else → diagnostics_writeback → finalize → END

The skill library at agent-workspace/domain-skills/ is reachable from the
agent loop via the lookup_skill/propose_skill/flush_skills MCP tools, so
learning is preserved across queries.

diagnostics_writeback drops a per-query JSON summary to
agent-workspace/diagnostics/<date>/<query_id>.json for offline consumption
by an external ReviewAgent (a code/prompt drift signal). It runs once on the
exit branch, right before finalize. URL-classification learning lives in the
skill library + its closed-loop correction, not in the graph.
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from src.config import settings
from src.models.state import AgentState
from src.services.context_compressor import ContextCompressor
from src.services.run_logger import log_event

logger = logging.getLogger(__name__)

# Initialize context compressor from centralized config.
# (Kept around because parse_intent/judge/reflect have long prompts; the
# agentic node manages its own context inside the Agent SDK loop.)
context_compressor = ContextCompressor(max_context_tokens=settings.compression.max_context_tokens)


def _should_continue(state: AgentState) -> str:
    """Conditional edge: decide whether to loop back into agent or proceed."""
    critic = state.get("critic_feedback")
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 3)

    # Capture the critic's verdict + gaps for the run log. This is the
    # single most informative signal for understanding multi-iter runs;
    # without it the log just shows "agentic_discovery ran twice" with
    # no explanation of WHY.
    is_sufficient = getattr(critic, "is_sufficient", None) if critic else None
    gaps = list(getattr(critic, "gaps", []) or []) if critic else []
    next_round_feedback = getattr(critic, "next_round_feedback", "") if critic else ""

    if critic and not critic.is_sufficient and iteration < max_iter:
        logger.info("Reflect: gaps found, looping back to agentic_discovery (iter %d/%d)",
                    iteration, max_iter)
        log_event("reflect_decision", {
            "iteration": iteration,
            "max_iterations": max_iter,
            "is_sufficient": is_sufficient,
            "gaps": gaps[:10],  # cap; critic occasionally emits long lists
            "next_round_feedback": (next_round_feedback or "")[:600],
            "next_action": "agentic_discovery",
            "reason": "gaps_found",
        })
        return "agentic_discovery"

    logger.info("Reflect: sufficient or max iterations reached; heading to diagnostics_writeback → finalize")
    log_event("reflect_decision", {
        "iteration": iteration,
        "max_iterations": max_iter,
        "is_sufficient": is_sufficient,
        "gaps": gaps[:10],
        "next_action": "diagnostics_writeback",
        "reason": "sufficient" if is_sufficient else "max_iterations_reached",
    })
    return "diagnostics_writeback"


def build_graph() -> StateGraph:
    """Construct the agentic-hybrid discovery graph."""
    from src.agents.nodes.agentic_discovery import agentic_discovery_node
    from src.agents.nodes.diagnostics_writeback import diagnostics_writeback_node
    from src.agents.nodes.finalize import finalize_node
    from src.agents.nodes.judge import judge_node
    from src.agents.nodes.parse_intent import parse_intent_node
    from src.agents.nodes.reflect import reflect_node

    graph = StateGraph(AgentState)

    # === 6 nodes ===
    graph.add_node("parse_intent", parse_intent_node)
    graph.add_node("agentic_discovery", agentic_discovery_node)
    graph.add_node("judge", judge_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("diagnostics_writeback", diagnostics_writeback_node)
    graph.add_node("finalize", finalize_node)

    # === Linear edges ===
    graph.add_edge("parse_intent", "agentic_discovery")
    graph.add_edge("agentic_discovery", "judge")
    graph.add_edge("judge", "reflect")

    # === Conditional edge: reflect → loop back to agent OR exit branch ===
    graph.add_conditional_edges(
        "reflect",
        _should_continue,
        {
            "agentic_discovery": "agentic_discovery",
            "diagnostics_writeback": "diagnostics_writeback",
        },
    )

    # Exit branch: diagnostics_writeback (per-query summary for offline
    # ReviewAgent) → finalize. skill_writeback removed 2026-06-04 — it was
    # inert (its `classify_prediction_types` input was never populated); skill
    # correction now flows through the closed loop (skill_library + bridge).
    graph.add_edge("diagnostics_writeback", "finalize")
    graph.add_edge("finalize", END)

    graph.set_entry_point("parse_intent")
    return graph


def compile_graph(checkpointer=None):
    graph = build_graph()
    if checkpointer is None:
        checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = compile_graph()
    return _compiled_graph
