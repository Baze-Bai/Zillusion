"""Stage 7: Reflect & Gap Check — Critic agent reviews coverage and quality.

If insufficient: generates feedback for next round (max 3 iterations).
If sufficient: passes through to finalize.

Principle: 80% coverage with acceptable quality → return.
Only loop if there are specific, actionable gaps.
"""

from __future__ import annotations

import logging
import time

from src.config import settings
from src.models.report import CriticOutput
from src.models.state import AgentState
from src.services.llm import llm_service
from src.services.run_logger import log_event

logger = logging.getLogger(__name__)

CRITIC_SYSTEM = """You are a data source discovery quality critic. Evaluate whether the discovered
data sources adequately cover the user's data needs.

Evaluate:
1. COVERAGE: Do the sources collectively cover all aspects of the user's requirement?
2. TYPE DIVERSITY: Are there API, file, and embedded data options?
3. QUALITY: Are the top sources authoritative and fresh?
4. GAPS: What specific data aspects are NOT covered?

Be pragmatic:
- 80% coverage with good quality → SUFFICIENT
- Only flag gaps that are SPECIFIC and ACTIONABLE (e.g., "missing government official data" not "could be better")
- Hard cap: max 3 iterations total. Don't suggest another round unless there's a clear, fixable gap.
"""

CRITIC_PROMPT = """User requirement: {requirement}
Iteration: {iteration} / {max_iterations}

Discovered sources ({count} total):
{sources_summary}

Score distribution:
- Top score: {top_score}
- Median score: {median_score}
- Type breakdown: {type_breakdown}

Is this result set SUFFICIENT for the user's needs?
If NOT, what specific gaps exist and how should the next search round address them?
"""


async def reflect_node(state: AgentState) -> dict:
    """Critic reviews discovery results and decides whether to loop."""
    start = time.monotonic()
    scored = state.get("scored_sources", [])
    requirement = state["requirement"]
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 3)

    # If no sources found, always try again (unless at max)
    if not scored and iteration < max_iter:
        logger.info("Stage 7: No sources found, requesting retry (iter %d/%d)", iteration, max_iter)
        log_event("reflect.critic_output", {
            "iteration": iteration + 1,
            "max_iterations": max_iter,
            "path": "no_sources_auto_retry",
            "is_sufficient": False,
            "coverage_analysis": "No data sources were discovered.",
            "gaps": ["Complete search failure — retry with broader keywords"],
            "next_round_feedback": "Broaden search keywords and try additional registries",
            "n_scored_input": 0,
            "forced_sufficient": False,
        })
        return {
            "critic_feedback": CriticOutput(
                is_sufficient=False,
                coverage_analysis="No data sources were discovered.",
                gaps=["Complete search failure — retry with broader keywords"],
                next_round_feedback="Broaden search keywords and try additional registries",
            ),
            "iteration": iteration + 1,
            "stage_timings": {**state.get("stage_timings", {}), "reflect": time.monotonic() - start},
        }

    # Build summary for critic
    sources_summary = "\n".join(
        f"- [{s.source_type.value}] {s.name} ({s.url[:60]}) — score: {s.scores.overall:.1f}"
        for s in scored[:20]
    ) if scored else "None"
    logger.debug("Stage 7 sources summary:\n%s", sources_summary)

    type_counts = {}
    for s in scored:
        t = s.source_type.value
        type_counts[t] = type_counts.get(t, 0) + 1

    scores = [s.scores.overall for s in scored if s.scores]
    top_score = f"{max(scores):.1f}" if scores else "N/A"
    median_score = f"{sorted(scores)[len(scores)//2]:.1f}" if scores else "N/A"
    logger.debug("Stage 7 score stats: top=%s, median=%s, count=%d, distribution=%s",
                  top_score, median_score, len(scores),
                  {f"{i}-{i+1}": sum(1 for s in scores if i <= s < i+1) for i in range(0, 11)} if scores else {})

    prompt = CRITIC_PROMPT.format(
        requirement=f"{requirement.original_query}\nDomain: {requirement.domain}",
        iteration=iteration + 1,
        max_iterations=max_iter,
        count=len(scored),
        sources_summary=sources_summary,
        top_score=top_score,
        median_score=median_score,
        type_breakdown=type_counts,
    )
    logger.debug("Stage 7 critic prompt:\n%s", prompt[:800])

    try:
        critic_output, usage = await llm_service.complete_structured(
            messages=[
                {"role": "system", "content": CRITIC_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_model=CriticOutput,
            profile="reflect",
            model_tier="reasoning",
            # max_tokens=None ⇒ no artificial cap; provider's own ceiling applies
            # (deepseek-v4-pro: 8192 output tokens). The previous cap of 1024 was
            # too tight when scored_sources >= 20 — CriticOutput's
            # coverage_analysis alone consumed ~1000 tokens, leaving the
            # next_round_feedback field truncated mid-string and breaking the
            # whole reflect node.
            max_tokens=None,
        )
    except Exception as e:  # noqa: BLE001
        # The critic is a QUALITY GATE, not the result producer — the scored
        # sources already exist in state. A flaky/timed-out reasoning provider
        # here must NOT discard that work. Fall back to force-sufficient (the
        # same call the max-iteration path makes) so the pipeline proceeds to
        # finalize with the results in hand instead of crashing the whole run.
        logger.warning(
            "Stage 7 critic LLM failed (%s) — force-sufficient, proceeding with results in hand",
            e,
        )
        log_event("reflect.critic_error", {
            "iteration": iteration + 1,
            "error_type": type(e).__name__,
            "message": str(e)[:500],
            "n_scored_input": len(scored),
        })
        from src.services.llm import LLMUsage

        critic_output = CriticOutput(
            is_sufficient=True,
            coverage_analysis=f"critic unavailable ({type(e).__name__}); proceeding with current results",
            gaps=[],
        )
        usage = LLMUsage()

    logger.debug(
        "Stage 7 critic output:\n"
        "  is_sufficient=%s\n"
        "  coverage_analysis=%s\n"
        "  gaps=%s\n"
        "  next_round_feedback=%s",
        critic_output.is_sufficient,
        (getattr(critic_output, 'coverage_analysis', '') or '')[:200],
        critic_output.gaps,
        (getattr(critic_output, 'next_round_feedback', '') or '')[:200],
    )

    # Force sufficient if at max iterations
    forced_sufficient = False
    if iteration + 1 >= max_iter:
        if not critic_output.is_sufficient:
            forced_sufficient = True
        critic_output.is_sufficient = True
        logger.info("Stage 7: Max iterations reached, forcing sufficient")

    duration = time.monotonic() - start
    logger.info(
        "Stage 7 complete: sufficient=%s, gaps=%d, iter=%d/%d, %.1fs",
        critic_output.is_sufficient, len(critic_output.gaps), iteration + 1, max_iter, duration,
    )

    log_event("reflect.critic_output", {
        "iteration": iteration + 1,
        "max_iterations": max_iter,
        "path": "llm_critic",
        "is_sufficient": critic_output.is_sufficient,
        "coverage_analysis": (getattr(critic_output, "coverage_analysis", "") or "")[:800],
        "gaps": list(critic_output.gaps or []),
        "next_round_feedback": (getattr(critic_output, "next_round_feedback", "") or "")[:800],
        "n_scored_input": len(scored),
        "score_top": top_score,
        "score_median": median_score,
        "type_breakdown": type_counts,
        "forced_sufficient": forced_sufficient,
        "duration_ms": round(duration * 1000, 1),
    })

    return {
        "critic_feedback": critic_output,
        "iteration": iteration + 1,
        "cost_accumulated": state.get("cost_accumulated", 0) + usage.cost_usd,
        "stage_timings": {**state.get("stage_timings", {}), "reflect": duration},
    }
