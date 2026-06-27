"""Stage 6: Two-Stage Judging — Haiku batch filter → Sonnet 5-dimension scoring.

Stage-A: Haiku processes 10 candidates per batch, scores only relevance.
         Candidates with relevance < 5 are eliminated.
Stage-B: Sonnet evaluates top-30 survivors with full 5-dimension scoring
         plus hard veto rules.
"""

from __future__ import annotations

import logging
import time

from src.config import settings
from src.judging.deterministic import score_accessibility, score_freshness, score_license_fit
from src.judging.stage_a import stage_a_filter
from src.judging.stage_b import stage_b_score
from src.models.data_source import DataSource
from src.models.scores import SourceScores
from src.models.state import AgentState

logger = logging.getLogger(__name__)


async def judge_node(state: AgentState) -> dict:
    """Two-stage judging pipeline (v3, 2026-05-22).

    v3 changes:
      - Stage-A now does binary filter + probe + relevance scoring.
      - portal_trees are injected into Stage-A as pseudo-sources so
        they get judged alongside flat sources.
      - Stage-B drops the relevance LLM call (Stage-A produced it) and
        instead computes 4 standalone fit-dimensions + LLM ranker for
        the final ordering.
      - License veto routes through LLM confirmation.
    """
    start = time.monotonic()
    sources = state.get("deduplicated_sources", [])
    trees = state.get("portal_trees", [])
    requirement = state["requirement"]

    if not sources and not trees:
        return {
            "scored_sources": [],
            "stage_timings": {**state.get("stage_timings", {}), "judge": 0},
        }

    budget = settings.budget

    # Collect sub-stage timings into a single dict so diagnostics can
    # surface them per-step (stage_a.tree_inject / stage_a.binary_llm /
    # stage_a.probe / stage_a.scoring_llm / stage_b.dim_scoring /
    # stage_b.ranker etc.).
    sub_timings: dict[str, float] = {}

    # === Stage-A: blacklist → bypass → binary → probe → scoring ===
    logger.info(
        "Stage-A: Filtering %d sources + %d portal_trees",
        len(sources), len(trees),
    )
    logger.debug("Stage 6 input sources: %s", [(s.name[:40], s.url[:60]) for s in sources[:15]])
    stage_a_survivors = await stage_a_filter(
        sources=sources,
        requirement=requirement,
        batch_size=budget.stage_a_batch_size,
        threshold=budget.relevance_threshold_stage_a,
        trees=trees,
        out_timings=sub_timings,
    )
    logger.info("Stage-A: %d sources + %d trees → %d survivors",
                len(sources), len(trees), len(stage_a_survivors))
    logger.debug("Stage-A survivors: %s", [(s.name[:40], s.url[:60]) for s in stage_a_survivors[:15]])

    # Cap at max_candidates_stage_b
    stage_b_candidates = stage_a_survivors[:budget.max_candidates_stage_b]
    if len(stage_a_survivors) > budget.max_candidates_stage_b:
        logger.debug("Stage-B capped: %d → %d (max_candidates_stage_b=%d)",
                      len(stage_a_survivors), len(stage_b_candidates), budget.max_candidates_stage_b)

    # === Stage-B: Sonnet 5-dimension scoring ===
    logger.info("Stage-B: Scoring %d candidates with Sonnet", len(stage_b_candidates))
    scored = await stage_b_score(
        sources=stage_b_candidates,
        requirement=requirement,
        out_timings=sub_timings,
    )

    # Log detailed scoring breakdown
    for s in scored:
        if s.scores:
            logger.debug(
                "Stage-B score: %s → rel=%.1f auth=%.1f fresh=%.1f access=%.1f lic=%.1f overall=%.1f",
                s.name[:40], s.scores.relevance, s.scores.authority,
                s.scores.freshness, s.scores.accessibility,
                s.scores.license_fit, s.scores.overall,
            )

    # Sort by overall score descending, filter out vetoed (-1)
    scored_alive = [s for s in scored if s.scores and s.scores.overall > 0]
    vetoed = [s for s in scored if s.scores and s.scores.overall <= 0]
    if vetoed:
        logger.debug("Stage 6 vetoed sources: %s",
                      [(s.name[:40], s.scores.overall if s.scores else 0) for s in vetoed])
    scored_alive.sort(key=lambda s: s.scores.overall if s.scores else 0, reverse=True)

    duration = time.monotonic() - start
    logger.info(
        "Stage 6 complete: %d input → %d stage-A → %d scored → %d alive, %.1fs",
        len(sources), len(stage_a_survivors), len(stage_b_candidates), len(scored_alive), duration,
    )
    # Surface the sub-stage breakdown so operators can see where time
    # went without grepping logs. Total ``judge`` stays as the
    # overall stage timing; sub-keys give the breakdown.
    logger.info(
        "Stage 6 timing breakdown: " + ", ".join(
            f"{k}={v:.2f}s" for k, v in sorted(sub_timings.items())
        )
    )

    # Merge sub-stage timings into state[stage_timings] alongside the
    # overall ``judge`` key. diagnostics_writeback will pick them up.
    merged_timings = {**state.get("stage_timings", {}), "judge": duration}
    merged_timings.update(sub_timings)

    return {
        "scored_sources": scored_alive,
        "stage_timings": merged_timings,
    }
