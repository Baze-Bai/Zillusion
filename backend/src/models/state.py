"""LangGraph AgentState — the central state flowing through all pipeline nodes.

Slimmed for the agentic-hybrid pipeline. The 9-node deterministic middle
section was collapsed into a single ``agentic_discovery`` node, so all the
intermediate fields it produced (filtered_candidates / portal_candidates /
expanded_children / type_classified / processed_candidates) are gone.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

import operator

from src.models.data_source import DataSource
from src.models.page_tree import DataPageTree
from src.models.report import CriticOutput, FinalReport
from src.models.requirement import StructuredRequirement


class PortalProfilerBudget(TypedDict, total=False):
    """Per-query budget for portal expansion inside the agentic loop.

    The agent reads these limits via the ``query_registry`` /
    ``firecrawl_map`` tool implementations (which check
    ``state.portal_budget`` indirectly through settings.budget) and via
    system-prompt budgets.
    """

    max_portals_to_map: int          # default 5
    max_scrape_per_portal: int       # default 15
    max_total_scrape: int            # default 40
    portals_mapped: int              # current count
    total_scraped: int               # current count


class AgentState(TypedDict, total=False):
    """Central state flowing through the agentic-hybrid pipeline."""

    # === Input ===
    query: str
    user_id: str

    # === Tracing / correlation ===
    request_id: str
    query_id: str

    # === Stage 1: Intent Parser ===
    requirement: StructuredRequirement

    # === Stage 2-5: Agentic Discovery (replaces 9 old nodes) ===
    # The agentic_discovery_node writes both normalized_sources and
    # deduplicated_sources (same list) so anything downstream that read
    # either field stays functional.
    normalized_sources: list[DataSource]
    deduplicated_sources: list[DataSource]
    # DataPageTree per portal the agent expanded — populated when the
    # agent's final JSON contains a `portal_trees` block. Flat sources
    # whose URLs appear inside any tree are removed from
    # normalized/deduplicated_sources during cross-structure dedup
    # (see runner._apply_cross_structure_dedup), so trees and flat list
    # never overlap.
    portal_trees: list[DataPageTree]

    # === Stage 6: Judge ===
    scored_sources: list[DataSource]

    # === Stage 7: Reflect ===
    critic_feedback: CriticOutput

    # === Stage 8.5: Skill Writeback ===
    # (no dedicated state field; writes to disk via SkillLibrary)

    # === Stage 8: Finalize ===
    final_report: FinalReport

    # === Control Flow ===
    iteration: int
    max_iterations: int
    portal_budget: PortalProfilerBudget

    # === Observability ===
    cost_accumulated: float
    stage_timings: dict[str, float]
    errors: Annotated[list[dict], operator.add]
