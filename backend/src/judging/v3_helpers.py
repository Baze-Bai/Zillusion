"""v3 judging helpers (2026-05-22).

Two pieces the v3 architecture adds on top of the dimension scorers:

  1. ``tree_to_pseudo_source``  — convert a DataPageTree into a DataSource
     stub so Stage-A / Stage-B treat trees alongside flat sources.
  2. ``llm_rank_sources``       — final ranking step that replaces the
     hand-tuned weighted sum with an LLM consuming all per-dim scores.

License veto stays pure-rule (see deterministic.py). An earlier draft
added an LLM-confirmation layer that was reverted on the same day —
license compatibility is a legal-compliance task; LLM hallucination here
risks letting a vetoed source through. Improve the alias table instead.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from pydantic import BaseModel, Field

from src.config import settings
from src.models.data_source import (
    AccessLevel,
    DataSource,
    DataSourceType,
)
from src.models.page_tree import DataPageTree
from src.services.llm import llm_service
from src.services.run_logger import log_event

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# tree_to_pseudo_source
# ──────────────────────────────────────────────────────────────────────


def tree_to_pseudo_source(tree: DataPageTree, tree_index: int = 0) -> DataSource:
    """Convert a DataPageTree to a DataSource stub for unified judging.

    A pseudo-source's metadata carries ``is_tree=True`` so downstream
    code can distinguish it. The .embedded_spec is intentionally None —
    most Stage-B dim scorers (format_fit / schema_coverage_fit) gracefully
    handle missing spec by returning their "no penalty" multipliers.

    license_fit on a tree pseudo-source is "unknown" — we don't propagate
    license claims from individual leaves up to the tree level.

    Metadata keys lifted from the tree (for Stage-A consumption without
    digging into ``_original_tree``):

      - is_tree                  (bool)   — marker
      - tree_index               (int)
      - tree_root_url            (str)
      - tree_n_children          (int)
      - tree_record_count        (int)
      - tree_fields_available    (list[str])
      - tree_crawl_session_id    (str)
      - tree_summary             (str)    ← agent's narrative; used as evidence
      - tree_is_sampled          (bool)   ← root actually fetched vs URL-inferred
      - tree_sampled_pages       (int)    ← detail pages actually crawled
      - tree_total_pages         (int)    ← total claimed by the tree
      - tree_page_type           (str)    ← root page_type: list/detail/category/hub
      - _original_tree           (dict)   ← full tree dump for finalize

    The synthesized ``description`` packs together the tree's structural
    info (record_count, fields_available, child count) so Stage-A's
    binary filter and downstream nodes have enough signal at-a-glance.
    """
    root = tree.root
    children = list(root.children or [])
    n_children = len(children)
    record_count = root.record_count or tree.total_detail_pages or 0

    # ── child_page_types: heterogeneity summary ────────────────────────
    # Count how many children of each page_type. Only useful when ≥2
    # distinct types appear (otherwise it's just "10 detail" which is
    # already captured by n_subpages). Kept here regardless; scoring
    # serializer decides whether to surface to LLM.
    child_page_types: dict[str, int] = {}
    for child in children:
        pt = (child.page_type or "").strip()
        if pt:
            child_page_types[pt] = child_page_types.get(pt, 0) + 1

    # ── field_progression: data-depth summary ──────────────────────────
    # Tree.field_progression is a dict like
    #   {"list_page": ["name","price"], "detail_page": ["name","price","address","reviews"]}
    # showing what fields appear at each tree level. Strong signal for
    # "is the data shallow or deep". Cap fields per layer to 10 to bound
    # prompt size.
    field_progression: dict[str, list[str]] = {}
    for layer, fields in (tree.field_progression or {}).items():
        if not fields:
            continue
        field_progression[str(layer)] = [str(f) for f in list(fields)[:10] if f]

    # Synthesize description from tree structure
    desc_parts = []
    if tree.tree_summary:
        desc_parts.append(tree.tree_summary[:200])
    if record_count:
        desc_parts.append(f"~{record_count} records")
    if root.fields_available:
        desc_parts.append(f"fields: {', '.join(root.fields_available[:8])}")
    if n_children:
        desc_parts.append(f"{n_children} subpages")
    description = "; ".join(desc_parts) or root.title or root.url

    # Stable ID from URL hash
    url_hash = hashlib.md5(root.url.encode("utf-8")).hexdigest()[:12]

    return DataSource(
        id=f"tree-{tree_index}-{url_hash}",
        name=root.title or root.url.split("/")[-1] or root.url,
        provider="",   # unknown from tree alone
        url=root.url,
        source_type=DataSourceType.EMBEDDED_DATA,
        description=description,
        domain=root.url.split("//")[-1].split("/")[0] if "//" in root.url else "",
        tags=["portal_tree"],
        data_format=[],
        access_level=AccessLevel.UNKNOWN,
        license=None,
        discovery_method="portal_tree",
        metadata={
            "is_tree": True,
            "tree_index": tree_index,
            "tree_root_url": root.url,
            "tree_n_children": n_children,
            "tree_record_count": record_count,
            "tree_fields_available": list(root.fields_available or []),
            "tree_crawl_session_id": tree.crawl_session_id,
            # Lift structural / narrative fields so Stage-A doesn't dig
            # into _original_tree.
            "tree_summary": (tree.tree_summary or "").strip(),
            "tree_is_sampled": bool(root.is_sampled),
            "tree_sampled_pages": int(tree.sampled_detail_pages or 0),
            "tree_total_pages": int(tree.total_detail_pages or 0),
            "tree_page_type": (root.page_type or "").strip(),
            # Children-summary signals (2026-05-22). NOT individual
            # children — those go into _original_tree only. These two
            # capture heterogeneity + data depth, the only aspects worth
            # surfacing to Stage-A's relevance LLM.
            "tree_child_page_types": child_page_types,
            "tree_field_progression": field_progression,
            # Stash the original tree dict so finalize can recover it
            "_original_tree": tree.model_dump(mode="json"),
        },
    )


def is_tree_source(source: DataSource) -> bool:
    """True if this DataSource was synthesized from a portal_tree."""
    return bool(source.metadata.get("is_tree", False))


# ──────────────────────────────────────────────────────────────────────
# LLM ranker (final step replacing the weighted sum)
# ──────────────────────────────────────────────────────────────────────


class _RankedItem(BaseModel):
    id: str = Field(description="Source id matching one of the inputs")
    rank: int = Field(ge=1, description="1 = best, 2 = next, ...")
    overall: float = Field(ge=0, le=10, description="Final overall 0-10 score for display")
    rationale: str = Field(default="", description="1-2 sentences explaining rank position")
    # Filter capability (2026-05-22). When drop=True, the source is
    # excluded from final results. Honored only when LLM filtering is
    # enabled in config, AND only up to max_drop_ratio of input. The
    # drop_reason field is required when dropping to make decisions
    # auditable.
    drop: bool = Field(
        default=False,
        description="True if this source should be filtered out (not just low-ranked).",
    )
    drop_reason: str = Field(
        default="",
        description="Required when drop=true. Must cite specific dim weaknesses.",
    )


class _RankerVerdict(BaseModel):
    ranked: list[_RankedItem] = Field(
        description="All input sources, with rank assigned. Drops marked via drop=true."
    )


def _serialize_source_for_ranker(source: DataSource) -> dict[str, Any]:
    """Compact dict representation for the LLM ranker prompt.

    Includes:
      - identity (id, name, url, source_type)
      - dim scores (relevance / authority / accessibility / license_fit /
        format_fit / temporal_fit / geographic_fit / schema_coverage_fit)
      - rationales (relevance_rationale, authority_rationale)
      - structural facts (is_tree, fields_available count, has_spec)
    """
    s = source.scores
    out: dict[str, Any] = {
        "id": source.id,
        "name": source.name[:80],
        "url": source.url[:120],
        "type": source.source_type.value,
        "description": (source.description or "")[:200],
    }
    if s:
        out["scores"] = {
            "relevance": round(s.relevance, 2),
            "authority": round(s.authority, 2),
            "accessibility": round(s.accessibility, 2),
            "license_fit": round(s.license_fit, 2),
            "format_fit": round(s.format_fit, 2),
            "temporal_fit": round(s.temporal_fit, 2),
            "geographic_fit": round(s.geographic_fit, 2),
            "schema_coverage": round(s.schema_coverage_fit, 2),
        }
        if s.relevance_rationale:
            out["why_relevant"] = s.relevance_rationale[:150]
    if is_tree_source(source):
        out["is_portal_tree"] = True
        out["n_subpages"] = source.metadata.get("tree_n_children", 0)
        out["record_count"] = source.metadata.get("tree_record_count", 0)
    return out


async def llm_rank_sources(
    sources: list[DataSource],
    user_query: str,
) -> list[DataSource]:
    """Final LLM ranking step.

    Input: list of DataSource objects with all dim scores populated.
    Output: same list, sorted by LLM-decided rank, with each source's
    ``scores.overall`` set to the LLM's rating and ``scores.rank_rationale``
    explaining the position.

    Single LLM call sees all survivors at once for cross-comparison.
    Conservative fallback on LLM failure: return list unchanged (Stage-B
    caller can fall back to weighted sum if it wants).
    """
    if not sources:
        return []
    if not settings.scoring.enable_stage_b_llm_ranker:
        return sources    # caller handles weighted-sum fallback

    payload = [_serialize_source_for_ranker(s) for s in sources]
    n_input = len(payload)

    # Filtering policy parameters
    s_cfg = settings.scoring
    filter_enabled = s_cfg.enable_stage_b_llm_filtering
    min_keep = s_cfg.stage_b_llm_filter_min_keep
    max_drop = int(n_input * s_cfg.stage_b_llm_filter_max_drop_ratio)
    # Don't enable filtering when pool is too small to safely drop anything.
    if filter_enabled and n_input < min_keep:
        filter_enabled = False

    filter_section = ""
    if filter_enabled:
        filter_section = f"""
FILTERING (in addition to ranking):
You MAY also drop sources that clearly do not serve the user's data need.
Set ``drop: true`` and provide a specific ``drop_reason`` citing which
dimensions are weak. Bias STRONGLY toward keeping; only drop when:

  - 3+ dimensions are weak AND together suggest the source can't serve the
    user (e.g., low schema_coverage + low format_fit + low geographic_fit
    means the source doesn't have the right data shape for this query).
  - The source is a near-duplicate of a higher-ranked one (same publisher,
    same coverage, no unique value-add).
  - Description / evidence makes it clear the source is off-need despite
    the dimension scores looking OK (e.g., relevance scored 6 but the
    source is actually documentation, not data).

DO NOT drop just because:
  - One dimension is low (let the ranking show it)
  - License is restricted (already handled by hard rules; if license_fit > 0
    here, the source is legally usable for the user's constraint)
  - You're unsure — when in doubt, KEEP and rank lower

You can drop at most {max_drop} of the {n_input} input sources. If you
try to drop more, the system will only honor the first {max_drop} drop
decisions (sorted by lowest rank). The drop_reason field is mandatory
when drop=true; rationales like "low quality" or "not best fit" are
NOT specific enough — cite concrete dimensions.
"""
    else:
        filter_section = """
FILTERING: Do NOT set drop=true on any source. All input sources must be
included in the ranked output.
"""

    prompt = f"""You are the final ranker for a data-source discovery pipeline.
Given the user query, all surviving candidate sources, and their per-dimension
scores, produce a ranked list (best first).

USER QUERY: {user_query}

Each candidate has:
- relevance (0-10): topical match to query, judged in Stage-A
- authority (0-10): publisher trust
- accessibility (0-10): ease of access (open > paid > unknown)
- license_fit (0-10): legal usability
- format_fit (0-10): does the source have actionable spec (real endpoint /
  download URL / data fields)
- temporal_fit (0-10): does source's coverage span the requested time range
- geographic_fit (0-10): does source's coverage cover the requested regions
- schema_coverage (0-10): how many of the user's target fields are present

Portal-tree pseudo-sources (is_portal_tree=true) lack license/access info;
treat their license/access scores as "unknown" rather than zeros. Their value
comes from coverage breadth (n_subpages, record_count).

RANKING RULES:
1. relevance is the dominant signal — a source with relevance=8 should
   normally rank above one with relevance=5, regardless of other dims.
2. Authority, format_fit, schema_coverage matter as tiebreakers and to
   demote sources whose relevance scoring overlooked structural issues.
3. License_fit < 3 means restricted usage — these sources rank below
   permissive equivalents but stay in the list (user already vetoed the
   forbidden ones).
4. Portal trees with high coverage (n_subpages or record_count) outrank
   single flat sources of comparable relevance when the user needs many
   records.
{filter_section}
Output a JSON object:
{{
  "ranked": [
    {{"id": "<source_id>", "rank": 1, "overall": 8.5,
      "rationale": "...", "drop": false}},
    {{"id": "<source_id>", "rank": 2, "overall": 7.2,
      "rationale": "...", "drop": false}},
    {{"id": "<source_id>", "rank": 3, "overall": 4.0,
      "rationale": "...", "drop": true, "drop_reason": "..."}},
    ...
  ]
}}

Cover ALL input ids. Use ``overall`` 0-10 for display; not all scores
need to be unique but they should reflect the ordering.

Input ({n_input} candidates):
{payload}
"""

    try:
        verdict, _ = await llm_service.complete_structured(
            messages=[{"role": "user", "content": prompt}],
            response_model=_RankerVerdict,
            profile="judge_stage_b",
            model_tier="strong",
            # max_tokens=None ⇒ no artificial cap; same rationale as reflect.
            # The _RankerVerdict must list ALL N input sources with id+rank+
            # overall+rationale+drop. At 23 sources × ~130 tokens/source
            # the output runs 3-5K tokens and the previous 1536 cap
            # truncated mid-list, killing the ranker call. Observed in
            # uswx-da1f01 and v3test-9e97fc — 100% reproducible at
            # source_count ≥ 20. Provider's hard ceiling applies (deepseek-
            # v4-pro: 8192 output tokens) which comfortably fits any
            # realistic ranker output.
            max_tokens=None,
        )
    except Exception as exc:
        logger.warning(
            "LLM ranker failed (%s) — falling back to relevance-only sort",
            type(exc).__name__,
        )
        log_event("stage_b.ranker_failed", {
            "n_input": n_input,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:600],
            "error_module": type(exc).__module__,
            "fallback": "relevance_only_sort",
        })
        # Fallback: set overall = relevance so judge_node's `overall > 0`
        # filter doesn't accidentally drop everything. Without this, an
        # LLM rate-limit or transient error would silently kill the
        # entire result set — observed in v3test-9e97fc on 2026-05-25.
        for s in sources:
            if s.scores is not None:
                s.scores.overall = max(s.scores.relevance, 0.01)
                s.scores.rank_rationale = (
                    f"llm_ranker_failed_fallback_to_relevance "
                    f"({type(exc).__name__})"
                )
        return sorted(
            sources,
            key=lambda s: (s.scores.relevance if s.scores else 0),
            reverse=True,
        )

    # Build id → DataSource map
    by_id = {s.id: s for s in sources}

    # ── Honor LLM filter decisions, bounded by safety nets ───────────
    # Pre-compute which drops we honor:
    #   1. Sort all items by rank ASC (LLM's "best first" order)
    #   2. Among items with drop=True, keep at most max_drop of them
    #      (sorted by rank DESC = drop the worst-ranked first; the LLM
    #       should be self-consistent here, but if it tries to drop too
    #       many, we apply drops in reverse-rank order)
    #   3. If after drops survivors < min_keep, restore highest-ranked
    #      dropped items until min_keep is reached.
    items_sorted = sorted(verdict.ranked, key=lambda r: r.rank)
    drop_candidates = [it for it in items_sorted if it.drop and filter_enabled]
    # Sort drop candidates by rank DESC — when over-quota, drop worst first
    drop_candidates.sort(key=lambda r: r.rank, reverse=True)

    honored_drops: set[str] = set()
    refused_drops: list[str] = []
    if filter_enabled:
        # max_drop already computed above; honor up to that many drops
        for it in drop_candidates:
            if len(honored_drops) >= max_drop:
                refused_drops.append(it.id)
                continue
            # Require non-empty drop_reason; refuse silent drops
            if not (it.drop_reason or "").strip():
                refused_drops.append(it.id)
                logger.info(
                    "LLM ranker drop refused (empty drop_reason): %s",
                    it.id[:40],
                )
                continue
            honored_drops.add(it.id)

        # Floor: ensure at least min_keep survive
        n_after_drops = n_input - len(honored_drops)
        if n_after_drops < min_keep:
            need_to_restore = min_keep - n_after_drops
            # Restore the highest-ranked (best) items from the drop set
            sorted_by_rank_asc = sorted(
                [it for it in items_sorted if it.id in honored_drops],
                key=lambda r: r.rank,
            )
            for it in sorted_by_rank_asc[:need_to_restore]:
                honored_drops.discard(it.id)
                refused_drops.append(it.id)
                logger.info(
                    "LLM ranker drop refused (min_keep floor): %s",
                    it.id[:40],
                )

    # Log filter outcome
    if filter_enabled:
        logger.info(
            "LLM ranker filter: input=%d, dropped=%d (max=%d), refused=%d, kept=%d",
            n_input, len(honored_drops), max_drop, len(refused_drops),
            n_input - len(honored_drops),
        )

    ranked_out: list[DataSource] = []
    dropped_out: list[DataSource] = []
    seen_ids: set[str] = set()
    for item in items_sorted:
        src = by_id.get(item.id)
        if src is None:
            logger.debug("Ranker emitted unknown id=%r — skipping", item.id)
            log_event("stage_b.ranker_decision", {
                "id": item.id,
                "outcome": "unknown_id_skipped",
                "rank": item.rank,
                "overall": round(item.overall, 2),
                "rationale": (item.rationale or "")[:200],
                "llm_drop": item.drop,
                "llm_drop_reason": (item.drop_reason or "")[:200],
            })
            continue
        seen_ids.add(item.id)
        if src.scores is not None:
            src.scores.overall = max(0.0, min(10.0, item.overall))
            src.scores.rank_rationale = item.rationale[:300]
        if item.id in honored_drops:
            # Mark as filter-dropped; downstream judge_node filter
            # (overall > 0) removes them. Use -1 sentinel.
            if src.scores is not None:
                src.scores.overall = -1
                # Annotate why
                drop_msg = f"llm_filter_drop: {item.drop_reason[:200]}"
                if src.scores.rank_rationale:
                    src.scores.rank_rationale = (
                        drop_msg + " | original: " + src.scores.rank_rationale[:150]
                    )
                else:
                    src.scores.rank_rationale = drop_msg
            dropped_out.append(src)
            outcome = "dropped"
        else:
            ranked_out.append(src)
            outcome = "drop_refused" if item.drop else "kept"
        log_event("stage_b.ranker_decision", {
            "id": src.id,
            "name": src.name[:80],
            "source_kind": "tree" if is_tree_source(src) else "flat",
            "rank": item.rank,
            "overall": round(item.overall, 2),
            "rationale": (item.rationale or "")[:200],
            "llm_drop": item.drop,
            "llm_drop_reason": (item.drop_reason or "")[:200],
            "outcome": outcome,
        })

    # Append any sources the LLM omitted (defensive) at the end with
    # a low default overall and a flag in rationale.
    missing = [s for s in sources if s.id not in seen_ids]
    for src in missing:
        if src.scores is not None:
            src.scores.overall = 5.0
            src.scores.rank_rationale = "llm_ranker_omitted_default_5"
        ranked_out.append(src)
        log_event("stage_b.ranker_decision", {
            "id": src.id,
            "name": src.name[:80],
            "source_kind": "tree" if is_tree_source(src) else "flat",
            "rank": None,
            "overall": 5.0,
            "rationale": "llm_ranker_omitted_default_5",
            "llm_drop": False,
            "llm_drop_reason": "",
            "outcome": "omitted_by_llm",
        })

    # Append dropped sources at the end so callers that filter by
    # overall>0 (judge_node) eliminate them naturally. Keep them in the
    # return list so diagnostics_writeback can still see the drop trace.
    ranked_out.extend(dropped_out)

    logger.info(
        "LLM ranker: kept=%d, dropped=%d (omitted-default=%d)",
        len(ranked_out) - len(dropped_out), len(dropped_out), len(missing),
    )
    log_event("stage_b.ranker_summary", {
        "n_input": n_input,
        "n_kept": len(ranked_out) - len(dropped_out),
        "n_dropped_honored": len(dropped_out),
        "n_drops_refused": len(refused_drops),
        "n_omitted_by_llm": len(missing),
        "max_drop_quota": max_drop,
        "filter_enabled": filter_enabled,
    })
    return ranked_out
