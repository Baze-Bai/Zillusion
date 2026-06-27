"""Stage-A Judging: filter + relevance scoring (v3, 2026-05-22).

v3 architecture (latest):
  - Step 1: hard URL blacklist (wrappers/package docs)
  - Step 2: llm_prior bypass (parse_intent vetted, skip binary)
  - Step 3: small-pool bypass (≤5 non-prior → skip binary)
  - Step 4: BINARY filter (fast LLM, batched ≤10) — keep|drop
  - Step 5: probe_url liveness check — drop dead URLs
  - Step 6: RELEVANCE SCORING (strong LLM, batched) — 0-10 per survivor
      ★ NEW: previously Stage-B's job; moved here in v3.
  - Step 7: TREE injection — portal_trees join as pseudo-sources and
      go through the same pipeline (binary + probe + scoring).

Why v3:
  - Stage-A binary alone left Stage-B paying a Sonnet call per source for
    relevance; centralizing relevance scoring here makes Stage-B cheaper
    and keeps a single source of truth for "is this relevant".
  - portal_trees previously bypassed judging entirely; now they get a
    relevance score so finalize can surface them in ranked order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from pydantic import BaseModel, Field

from src.config import settings
from src.models.data_source import DataSource
from src.models.page_tree import DataPageTree
from src.models.requirement import StructuredRequirement
from src.models.scores import SourceScores
from src.services.llm import llm_service
from src.services.run_logger import log_event
from src.tools.validation.url_canonicalizer import canonicalize_url
from src.utils.json_parse import extract_json

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Topic context formatter (shared by binary + scoring prompts)
#
# Stage-A is responsible for "is this source on the user's topic?" — a
# pure semantic question. The fields included here are all TOPIC signals
# (what the user is searching for); deliberately NOT included are
# CONSTRAINT signals (geo / temporal / format / license / target_schema)
# because those are Stage-B's deterministic dimensions, and duplicating
# them here would either (a) let Stage-A drop sources for constraint
# reasons that Stage-B already handles, or (b) double-count signal.
#
# Empty fields are omitted to keep the prompt lean — a candidate batch
# of 10 sources gets ~3-4KB of context, well under fast tier limits.
# ──────────────────────────────────────────────────────────────────────


def _format_topic_context(requirement: StructuredRequirement) -> str:
    """Build the USER NEED block for Stage-A prompts.

    Lines:
      USER NEED: <original_query>
        domain: <domain>
        sub_domains: <comma-separated, when non-empty>
        keywords (zh): <comma-separated, when non-empty, capped to 10>
        keywords (en): <comma-separated, when non-empty, capped to 10>
        sub-questions:
          - <sq1>
          - <sq2>
          ...
    """
    lines: list[str] = [
        f"USER NEED: {requirement.original_query}",
        f"  domain: {requirement.domain}",
    ]
    if requirement.sub_domains:
        # Cap at 5 — more is noise for Stage-A's main job.
        sd = ", ".join(str(s) for s in requirement.sub_domains[:5] if s)
        if sd:
            lines.append(f"  sub_domains: {sd}")

    if requirement.search_keywords_zh:
        kw = ", ".join(str(k) for k in requirement.search_keywords_zh[:10] if k)
        if kw:
            lines.append(f"  keywords (zh): {kw}")

    if requirement.search_keywords_en:
        kw = ", ".join(str(k) for k in requirement.search_keywords_en[:10] if k)
        if kw:
            lines.append(f"  keywords (en): {kw}")

    if requirement.sub_questions:
        # Cap at 5 sub-questions; the agentic_discovery already used
        # the full list to search, here it's just for topic disambiguation.
        sqs = [str(q) for q in requirement.sub_questions[:5] if q]
        if sqs:
            lines.append("  sub-questions:")
            for q in sqs:
                lines.append(f"    - {q}")
    return "\n".join(lines)


# Prompt redesigned 2026-05-22.
#
# Rules followed:
#   - Binary output (no score). LLM answers "keep" or "drop", no number
#     to anchor on. Reason is a short phrase, not free-form rationale.
#   - No example value in the output schema (was "{score: 7.5}", which
#     anchored Layer 3 of authority similarly).
#   - No domain-specific rules in the prompt. Geographic / temporal /
#     format / wrapper checks are all deterministic in Stage-B + the
#     pre-Stage-A wrapper blacklist. Keeping them in the prompt only
#     duplicates signal and gives the LLM more ways to disagree with
#     the deterministic guards.
#   - Strict topic focus. The LLM is told to answer ONE question.
STAGE_A_PROMPT = """\
You are filtering candidates for a data-source discovery pipeline. For
each source, decide whether it is TOPICALLY RELEVANT to the user's
need — does this source plausibly contain (or directly link to) data
about the user's topic?

{topic_context}

The keywords and sub-questions above are different angles of the same
topic — a source is relevant if it matches ANY of them. Use bilingual
keywords to match sources written in either language.

Be CONSERVATIVE about dropping. Stage-B will do deeper evaluation on
every source you keep — your only job here is to drop sources that are
clearly off-topic (different subject entirely, unrelated domain). Do
NOT drop a source just because of minor quality issues, wrong region,
wrong time range, or format mismatch — those are Stage-B's checks.

For each source, output a single keep/drop decision with a brief reason
(under 12 words). No scores. No numbers.

Sources to evaluate:
{sources}

Output a JSON array, one object per source in input order:
[
  {{"index": 1, "keep": true, "reason": "<short reason>"}},
  {{"index": 2, "keep": false, "reason": "<short reason>"}},
  ...
]
"""


async def stage_a_filter(
    sources: list[DataSource],
    requirement: StructuredRequirement,
    batch_size: int = 10,  # noqa: ARG001  kept for back-compat; no longer used
    threshold: float = 5.0,  # noqa: ARG001  kept for back-compat
    trees: list[DataPageTree] | None = None,
    out_timings: dict[str, float] | None = None,
) -> list[DataSource]:
    """v3 Stage-A: blacklist → bypass → binary → probe → relevance scoring.

    Pipeline:
      1. ★ Tree injection — convert each DataPageTree into a pseudo-source.
      2. Wrapper-URL pre-filter (hard drop language wrappers / package docs).
      3. llm_prior bypass — parse_intent-vetted sources skip binary filter.
      4. Small-pool bypass — if ≤5 candidates, skip binary filter.
      5. Binary LLM decision on remaining non-prior candidates.
      6. probe_url liveness check.
      7. ★ Relevance scoring — strong LLM gives 0-10 per survivor.

    Output: list of DataSource (sources + tree pseudo-sources) with
    .scores.relevance populated. .scores.overall is left 0 — Stage-B's
    LLM ranker (or weighted-sum fallback) sets it.
    """
    if not sources and not trees:
        return []

    # Timing setup
    timings = out_timings if out_timings is not None else {}
    overall_start = time.monotonic()

    # ── 1. Tree injection ─────────────────────────────────────────────
    t0 = time.monotonic()
    sources = list(sources or [])
    n_real_sources = len(sources)
    n_trees_injected = 0
    pseudo_ids: list[str] = []
    inject_failures: list[dict] = []
    n_trees_in = len(trees or [])
    feature_enabled = bool(settings.scoring.enable_stage_a_tree_judge)
    if trees and feature_enabled:
        from src.judging.v3_helpers import tree_to_pseudo_source
        for i, t in enumerate(trees):
            try:
                pseudo = tree_to_pseudo_source(t, tree_index=i)
                sources.append(pseudo)
                n_trees_injected += 1
                pseudo_ids.append(pseudo.id)
            except Exception as e:
                inject_failures.append({
                    "tree_index": i,
                    "error_type": type(e).__name__,
                    "error": str(e)[:200],
                })
                logger.warning(
                    "Stage-A tree-injection failed for tree #%d: %s — skipping", i, e,
                )
        if n_trees_injected:
            logger.info(
                "Stage-A injected %d portal_trees as pseudo-sources "
                "alongside %d flat sources",
                n_trees_injected, n_real_sources,
            )
    timings["stage_a.tree_inject"] = time.monotonic() - t0
    log_event("stage_a.tree_inject", {
        "feature_enabled": feature_enabled,
        "n_trees_in": n_trees_in,
        "n_flat_sources": n_real_sources,
        "n_pseudo_out": n_trees_injected,
        "pseudo_ids": pseudo_ids,
        "failures": inject_failures,
        "elapsed_ms": round(timings["stage_a.tree_inject"] * 1000, 1),
    })

    if not sources:
        timings["stage_a.total"] = time.monotonic() - overall_start
        return []

    # ── 2. Pre-filter: wrapper / package-docs URLs ────────────────────
    t0 = time.monotonic()
    if settings.scoring.enable_wrapper_url_blacklist:
        from src.judging.deterministic import is_wrapper_or_docs_url
        before_blacklist = len(sources)
        sources = [s for s in sources if not is_wrapper_or_docs_url(s.url)]
        dropped = before_blacklist - len(sources)
        if dropped:
            logger.info(
                "Stage-A wrapper-URL blacklist: dropped %d/%d "
                "(R/Python wrappers, package docs, readthedocs)",
                dropped, before_blacklist,
            )
        if not sources:
            timings["stage_a.blacklist"] = time.monotonic() - t0
            timings["stage_a.total"] = time.monotonic() - overall_start
            return []
    timings["stage_a.blacklist"] = time.monotonic() - t0

    # ── 2.5 Dedup by canonical URL ────────────────────────────────────
    # The same landing page routinely arrives via several search engines /
    # adapters as distinct candidates; each duplicate previously consumed
    # its own binary-filter LLM call AND scoring call. Keep the first
    # occurrence in input order. Tree pseudo-sources are exempt — they
    # carry structure a flat candidate with the same URL doesn't.
    t0 = time.monotonic()
    _pseudo = set(pseudo_ids)
    _seen_canon: set[str] = set()
    _deduped: list = []
    for s in sources:
        if s.id in _pseudo:
            _deduped.append(s)
            continue
        try:
            canon = canonicalize_url(s.url) if s.url else s.id
        except Exception:  # noqa: BLE001 — fall back to a cheap normalization
            canon = (s.url or s.id).strip().lower().rstrip("/")
        if canon in _seen_canon:
            continue
        _seen_canon.add(canon)
        _deduped.append(s)
    n_url_dupes = len(sources) - len(_deduped)
    if n_url_dupes:
        logger.info(
            "Stage-A canonical-URL dedup: dropped %d/%d duplicate candidates",
            n_url_dupes, len(sources),
        )
        log_event("stage_a.url_dedup", {
            "n_in": len(sources), "n_dropped": n_url_dupes,
        })
    sources = _deduped
    timings["stage_a.url_dedup"] = time.monotonic() - t0

    # ── 3. Partition: llm_prior bypass ─────────────────────────────────
    llm_prior_sources = [
        s for s in sources
        if getattr(s, "discovery_method", "") == "llm_prior"
    ]
    candidates_for_llm = [
        s for s in sources
        if getattr(s, "discovery_method", "") != "llm_prior"
    ]
    if llm_prior_sources:
        logger.info(
            "Stage-A llm_prior bypass: %d candidates (parse_intent vetted) "
            "skip the binary filter",
            len(llm_prior_sources),
        )

    # ── 4. Small-pool bypass + 5. Binary LLM decision ─────────────────
    t0 = time.monotonic()
    if len(candidates_for_llm) <= 5:
        if candidates_for_llm:
            logger.info(
                "Stage-A small-pool bypass: only %d non-prior candidates, "
                "skipping binary filter",
                len(candidates_for_llm),
            )
        kept = candidates_for_llm
        timings["stage_a.binary_llm"] = 0.0    # bypassed
    else:
        kept = await _stage_a_binary_decide(candidates_for_llm, requirement)
        timings["stage_a.binary_llm"] = time.monotonic() - t0

    # Re-assemble in original input order
    survivor_ids = {s.id for s in kept} | {s.id for s in llm_prior_sources}
    survivors = [s for s in sources if s.id in survivor_ids]

    # ── 6. URL liveness probe ─────────────────────────────────────────
    t0 = time.monotonic()
    if settings.judging.enable_stage_a_probe:
        survivors = await _probe_filter(survivors)
    timings["stage_a.probe"] = time.monotonic() - t0

    # ── 7. ★ Relevance scoring (v3 new) ───────────────────────────────
    # Per-source scoring with up to `stage_a_scoring_max_concurrent` (100)
    # in-flight calls. Same per-source-parallel pattern as the binary
    # filter above (step 5); batch_size on stage_a_filter is now unused.
    t0 = time.monotonic()
    if settings.scoring.enable_stage_a_scoring and survivors:
        survivors = await _stage_a_score_relevance(survivors, requirement)
    timings["stage_a.scoring_llm"] = time.monotonic() - t0

    timings["stage_a.total"] = time.monotonic() - overall_start
    logger.info(
        "Stage-A timings: tree_inject=%.2fs, blacklist=%.2fs, "
        "binary_llm=%.2fs, probe=%.2fs, scoring_llm=%.2fs, total=%.2fs",
        timings.get("stage_a.tree_inject", 0),
        timings.get("stage_a.blacklist", 0),
        timings.get("stage_a.binary_llm", 0),
        timings.get("stage_a.probe", 0),
        timings.get("stage_a.scoring_llm", 0),
        timings.get("stage_a.total", 0),
    )
    return survivors


# ──────────────────────────────────────────────────────────────────────
# Internal: binary LLM decision
# ──────────────────────────────────────────────────────────────────────


async def _stage_a_binary_decide(
    candidates: list[DataSource],
    requirement: StructuredRequirement,
) -> list[DataSource]:
    """Run the binary LLM filter; returns the subset to keep.

    One LLM call per candidate, fanned out concurrently via asyncio.gather
    and bounded by ``settings.budget.stage_a_binary_max_concurrent`` (100).
    Per-source isolation means a single source's parse failure or rate-limit
    error no longer drags down the 9 others that previously shared its
    batch. Fast tier; per-call latency is ~1-3s so wall time approaches the
    slowest single call.

    Default-keep behavior is preserved: parse failures / LLM errors /
    omissions on a specific source default to ``keep=True`` with a tagged
    reason — Stage-A is intentionally conservative since Stage-B does the
    deeper evaluation.
    """
    decisions: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    sem = asyncio.Semaphore(settings.budget.stage_a_binary_max_concurrent)

    async def _decide_one(src: DataSource) -> None:
        async with sem:
            sources_text = _format_source_for_binary(1, src)
            prompt = STAGE_A_PROMPT.format(
                topic_context=_format_topic_context(requirement),
                sources=sources_text,
            )
            logger.debug(
                "Stage-A binary single src=%s prompt: %s",
                src.name[:40], prompt[:400],
            )
            try:
                response, _ = await llm_service.complete(
                    messages=[{"role": "user", "content": prompt}],
                    profile="judge_stage_a",
                    model_tier="fast",
                    max_tokens=settings.llm.max_tokens_stage_a,
                )
                logger.debug(
                    "Stage-A binary src=%s response: %s",
                    src.name[:40], response[:400],
                )
                parsed = extract_json(response, default=[])
                # Single-source response: take first array item if present,
                # else accept a bare dict (LLM sometimes drops the array
                # wrapper when N=1).
                item: dict | None = None
                if isinstance(parsed, list) and parsed:
                    cand = parsed[0]
                    if isinstance(cand, dict):
                        item = cand
                elif isinstance(parsed, dict):
                    item = parsed
                if item is None:
                    decisions[src.id] = True
                    reasons[src.id] = "llm_parse_failed_keep_all"
                    return
                keep = bool(item.get("keep", True))   # default keep on missing
                decisions[src.id] = keep
                reasons[src.id] = str(item.get("reason", ""))[:60]
            except Exception as e:
                logger.warning(
                    "Stage-A binary failed for %s: %s — keeping",
                    src.name[:40], e,
                )
                decisions[src.id] = True
                reasons[src.id] = f"llm_error_keep:{type(e).__name__}"

    await asyncio.gather(*[_decide_one(s) for s in candidates])

    kept = [s for s in candidates if decisions.get(s.id, True)]
    dropped = [s for s in candidates if not decisions.get(s.id, True)]
    logger.info(
        "Stage-A binary filter: kept %d/%d (dropped %d)",
        len(kept), len(candidates), len(dropped),
    )
    if dropped:
        sample = [(s.name[:40], reasons.get(s.id, "")) for s in dropped[:5]]
        logger.debug("Stage-A dropped samples: %s", sample)
    for s in candidates:
        keep = decisions.get(s.id, True)
        log_event("stage_a.candidate_decided", {
            "stage": "binary",
            "id": s.id,
            "name": s.name[:80],
            "url": s.url[:120],
            "source_kind": "tree" if (s.metadata or {}).get("is_tree") else "flat",
            "discovery_method": getattr(s, "discovery_method", "") or "",
            "decision": "keep" if keep else "drop",
            "reason": reasons.get(s.id, "")[:200],
        })
    return kept


# ──────────────────────────────────────────────────────────────────────
# Internal: URL liveness probe
# ──────────────────────────────────────────────────────────────────────


async def _probe_filter(sources: list[DataSource]) -> list[DataSource]:
    """Probe every survivor with HEAD/GET; drop ones the prober flags dead.

    A source is considered dead when:
      - probe.is_alive == False (transport error / DNS failure / timeout),
        OR
      - status_code >= 400 AND not in 401/403 (401/403 = auth required,
        page exists; downstream judge can still evaluate the spec/metadata)
      - status_code in {404, 410} (resource genuinely missing)

    We're deliberately lenient: a single HEAD probe can give false-negatives
    on WAF-protected hosts; the prober already retries via streamed GET. If
    even GET fails (or returns 404), we drop the candidate so it doesn't
    waste a Sonnet call.
    """
    from src.tools.validation.head_prober import probe_url

    timeout = settings.judging.stage_a_probe_timeout

    async def _probe_one(src: DataSource) -> tuple[DataSource, bool, str, int | None, float]:
        t_start = time.monotonic()
        try:
            result = await probe_url(src.url, timeout=timeout)
        except Exception as e:
            return src, False, f"probe_exception:{type(e).__name__}", None, (time.monotonic() - t_start) * 1000
        elapsed = (time.monotonic() - t_start) * 1000
        if not result.is_alive:
            return src, False, f"dead:status={result.status_code}", result.status_code, elapsed
        # Hard-drop 404 / 410 regardless of is_alive (some probers report
        # 404 as "alive" because the response came back).
        if result.status_code in (404, 410):
            return src, False, f"hard_drop:status={result.status_code}", result.status_code, elapsed
        return src, True, f"alive:status={result.status_code}", result.status_code, elapsed

    results = await asyncio.gather(
        *[_probe_one(s) for s in sources],
        return_exceptions=False,
    )
    kept = [src for src, alive, _, _, _ in results if alive]
    dropped = [(src, reason) for src, alive, reason, _, _ in results if not alive]

    for src, alive, reason, status_code, elapsed_ms in results:
        log_event("stage_a.probe_result", {
            "id": src.id,
            "url": src.url[:200],
            "alive": alive,
            "status_code": status_code,
            "reason": reason,
            "elapsed_ms": round(elapsed_ms, 1),
            "source_kind": "tree" if (src.metadata or {}).get("is_tree") else "flat",
        })

    if dropped:
        logger.info(
            "Stage-A probe: dropped %d/%d dead URLs (samples: %s)",
            len(dropped), len(sources),
            [(s.name[:40], r) for s, r in dropped[:5]],
        )
    else:
        logger.info("Stage-A probe: all %d survivors alive", len(sources))
    return kept


# ──────────────────────────────────────────────────────────────────────
# v3: Relevance scoring step (★ NEW)
# ──────────────────────────────────────────────────────────────────────


# Prompt designed for STRONG tier (deepseek-pro / Sonnet) with rubric-based
# 0-10 scoring. Asks for batched output. Anchor-free output schema
# (the example uses placeholder `<float>` rather than a numeric value).
SCORING_PROMPT = """\
Score the RELEVANCE of each data source to the user's data need (0-10).

{topic_context}

The keywords and sub-questions above are different angles of the same
topic; score based on how well the source matches the COMBINATION of
these signals. A source that strongly matches one sub-question and
several keywords typically scores 7-9; one that only weakly aligns
scores 3-5.

RUBRIC:
  9-10: PERFECT match — exactly addresses the user's topic with the right
        kind of data; would be a top-1 recommendation.
  7-8:  STRONG match — main topic matches; quality and structure look fit
        for purpose.
  5-6:  PARTIAL match — topic adjacent or the source covers the topic but
        with significant gaps (wrong region, wrong granularity, wrong
        time range, etc.).
  3-4:  WEAK match — tangentially related; user would need substantial
        manual effort to extract value.
  0-2:  IRRELEVANT — different topic, off-domain.

Some candidates are PORTAL TREES (marked is_portal_tree=true). Score
them on their structural value as data sources:
  - high record_count + relevant fields_available + matching topic → 8-10
  - low record_count or unclear coverage → 5-7
  - field_progression showing list→detail field growth (e.g. list_page has
    [name, price] but detail_page adds [address, reviews, ratings]) → +1
    (data depth signal — tree exposes richer fields at leaf level)
  - child_page_types with multiple distinct types (e.g. {{detail: 5,
    review: 3, photo: 2}}) → +1 (multi-facet data, not just flat list)
  - is_sampled=false → -1 (URL-inferred, not actually crawled)

Output a JSON array, one object per source in input order, with the
EXACT id that was passed in:
[
  {{"id": "<source_id>", "relevance": <float 0-10>, "reason": "<2-4 short sentences>"}},
  ...
]

Sources to score:
{sources}
"""


class _ScoredItem(BaseModel):
    id: str = Field(description="Source id from input")
    relevance: float = Field(ge=0, le=10)
    reason: str = Field(default="")


class _ScoringVerdict(BaseModel):
    scored: list[_ScoredItem] = Field(default_factory=list)


def _extract_evidence(source: DataSource, cap: int = 250) -> str:
    """Pull narrative evidence about why this source matches the user need.

    Two paths depending on source kind:

      Flat source (agent-committed): reads ``metadata.evidence``, which
      the agent writes at commit_source time as a natural-language
      explanation often quoting concrete field names / record counts.

      Tree pseudo-source (synthesized in ``tree_to_pseudo_source``):
      ``metadata.evidence`` is empty (agent doesn't write it for trees)
      — falls back to ``metadata.tree_summary``, which the LLM wrote
      as a narrative description of the whole crawled portal_tree.
      This gives tree pseudo-sources an "evidence" channel symmetric
      with flat sources.

    Skip when the resolved text is empty or a duplicate of description.
    """
    meta = source.metadata or {}

    # Tree-specific path: tree_summary acts as evidence
    if meta.get("is_tree", False):
        raw = (meta.get("tree_summary") or "").strip()
    else:
        raw = str(meta.get("evidence", "") or "").strip()

    if not raw:
        return ""
    desc = (source.description or "").strip().lower()
    if raw.lower() == desc:
        return ""    # exact duplicate
    return raw[:cap]


def _format_source_for_binary(idx: int, source: DataSource) -> str:
    """Multi-line per-source block for the Stage-A binary prompt.

    Format (lines separated by ``\\n``):
        [idx] <name> (<type>) <url>
            desc: <description[:200]>
            evidence: <metadata.evidence[:250]>   ← only when present + distinct
    """
    lines = [
        f"[{idx}] {source.name} ({source.source_type.value}) {source.url}",
        f"    desc: {(source.description or '')[:200]}",
    ]
    evidence = _extract_evidence(source, cap=250)
    if evidence:
        lines.append(f"    evidence: {evidence}")
    return "\n".join(lines)


def _serialize_source_for_scoring(source: DataSource) -> dict:
    """Compact dict representation passed to the relevance LLM.

    Field surface (kept in sync with `_format_source_for_binary` for
    the binary filter):

    All sources:
      - id, name (≤80), url (≤120), type, description (≤300)
      - evidence (≤250, optional) — agent's "why relevant" narrative

    Tree pseudo-sources additionally get:
      - is_portal_tree         (bool)
      - n_subpages             (int)   — len(root.children)
      - record_count           (int)   — root.record_count or total_detail_pages
      - fields_available       (list)  — root.fields_available, capped to 12
      - page_type              (str)   — list / detail / category / hub / ...
      - is_sampled             (bool)  — root actually fetched (vs URL-inferred)
      - sampled_pages          (int)   — detail pages actually crawled
      - total_pages            (int)   — total pages claimed by the tree
      - child_page_types       (dict)  — heterogeneity summary, only when ≥2 distinct types
      - field_progression      (dict)  — list_page / detail_page field map, only when ≥2 layers

    Constraints / spec fields (license / access_level / format / geographic /
    target_schema) are deliberately NOT included — those are Stage-B's
    deterministic dimensions; surfacing them here would let Stage-A's
    relevance LLM second-guess Stage-B and double-count signal.
    """
    out = {
        "id": source.id,
        "name": source.name[:80],
        "url": source.url[:120],
        "type": source.source_type.value,
        "description": (source.description or "")[:300],
    }
    # Evidence: agent's natural-language "why relevant" — often quotes
    # concrete page snippets / field names / record counts useful for
    # the scoring rubric. For trees this is sourced from tree_summary.
    evidence = _extract_evidence(source, cap=250)
    if evidence:
        out["evidence"] = evidence
    # Tree pseudo-sources get extra structural info
    meta = source.metadata or {}
    if meta.get("is_tree", False):
        out["is_portal_tree"] = True
        out["n_subpages"] = int(meta.get("tree_n_children", 0))
        out["record_count"] = int(meta.get("tree_record_count", 0))
        fields = meta.get("tree_fields_available", []) or []
        if fields:
            out["fields_available"] = fields[:12]
        # Structural signals to help LLM tell a crawled tree apart from
        # a URL-inferred stub.
        page_type = meta.get("tree_page_type") or ""
        if page_type:
            out["page_type"] = page_type
        out["is_sampled"] = bool(meta.get("tree_is_sampled", False))
        sampled = int(meta.get("tree_sampled_pages", 0))
        if sampled:
            out["sampled_pages"] = sampled
        total = int(meta.get("tree_total_pages", 0))
        if total and total != out["record_count"]:
            out["total_pages"] = total

        # ── children-summary signals (2026-05-22) ─────────────────────
        # Only surface when actually informative — homogeneous trees
        # (1 page_type, 1 layer) don't get these fields so we avoid
        # noise / prompt bloat on the common case.
        child_types = meta.get("tree_child_page_types") or {}
        if len(child_types) >= 2:
            out["child_page_types"] = dict(child_types)
        fp = meta.get("tree_field_progression") or {}
        if len(fp) >= 2:
            out["field_progression"] = dict(fp)
    return out


async def _stage_a_score_relevance(
    survivors: list[DataSource],
    requirement: StructuredRequirement,
) -> list[DataSource]:
    """v3 step 7: assign each survivor a 0-10 relevance score via strong LLM.

    One LLM call per source, fanned out concurrently via asyncio.gather and
    bounded by ``settings.budget.stage_a_scoring_max_concurrent`` (default
    100). Compared to the previous N-per-batch approach, single-source
    scoring gives:
      - Cleaner failure isolation: one source's truncation no longer
        zeroes out scores for the 9 others sharing its batch.
      - True parallel wall time: ≈ slowest single-source call instead of
        (batches × per-batch latency).
      - Better prompt-cache amortization: the ~1.5K-token rubric prefix
        is identical across all calls, so DeepSeek cache hit-rate
        approaches 100% after the first call lands.

    Score is written to ``source.scores.relevance`` and a short reason
    to ``source.scores.relevance_rationale``. Other SourceScores fields
    are left at defaults; Stage-B fills them.

    Fallback: on LLM error or parse failure for one source, that source
    gets relevance=5.0 + "[scoring_failed]" rationale so Stage-B's
    existing sentinel-aware logic kicks in. Other sources unaffected.
    """
    if not survivors:
        return []

    scored_by_id: dict[str, tuple[float, str]] = {}
    sem = asyncio.Semaphore(settings.budget.stage_a_scoring_max_concurrent)

    async def _score_one(src: DataSource) -> None:
        async with sem:
            payload = [_serialize_source_for_scoring(src)]
            prompt = SCORING_PROMPT.format(
                topic_context=_format_topic_context(requirement),
                sources=json.dumps(payload, ensure_ascii=False, indent=2),
            )
            logger.debug(
                "Stage-A score single src=%s: %s",
                src.name[:40], prompt[:400],
            )
            try:
                verdict, _ = await llm_service.complete_structured(
                    messages=[{"role": "user", "content": prompt}],
                    response_model=_ScoringVerdict,
                    profile="judge_stage_a_scoring",
                    model_tier="strong",
                    max_tokens=settings.llm.max_tokens_stage_b,
                )
                # Single-source verdict: take the first (and usually only)
                # entry. Fall back to id-match if the LLM emitted multiple.
                item = next(
                    (it for it in verdict.scored if it.id == src.id),
                    verdict.scored[0] if verdict.scored else None,
                )
                if item is None:
                    scored_by_id[src.id] = (
                        5.0, "[scoring_failed] empty_verdict",
                    )
                else:
                    scored_by_id[src.id] = (
                        max(0.0, min(10.0, float(item.relevance))),
                        (item.reason or "")[:300],
                    )
            except Exception as exc:
                logger.warning(
                    "Stage-A scoring failed for %s (%s) — fallback 5.0",
                    src.name[:40], type(exc).__name__,
                )
                scored_by_id[src.id] = (
                    5.0,
                    f"[scoring_failed] llm_error_in_stage_a:{type(exc).__name__}",
                )

    await asyncio.gather(*[_score_one(s) for s in survivors])

    # Attach scores to each source via .scores (create stub SourceScores)
    for src in survivors:
        score, reason = scored_by_id.get(
            src.id, (5.0, "[scoring_failed] omitted_by_llm"),
        )
        # Initialize SourceScores if missing; downstream stage_b sets the rest
        if src.scores is None:
            src.scores = SourceScores(
                relevance=score,
                authority=5.0,
                freshness=5.0,
                accessibility=5.0,
                license_fit=5.0,
                overall=0.0,
                relevance_rationale=reason,
            )
        else:
            src.scores.relevance = score
            src.scores.relevance_rationale = reason
        log_event("stage_a.scoring_result", {
            "id": src.id,
            "name": src.name[:80],
            "url": src.url[:120],
            "source_kind": "tree" if (src.metadata or {}).get("is_tree") else "flat",
            "discovery_method": getattr(src, "discovery_method", "") or "",
            "relevance": round(score, 2),
            "reason": (reason or "")[:300],
            "scoring_failed": reason.startswith("[scoring_failed]") if isinstance(reason, str) else False,
        })

    logger.info(
        "Stage-A relevance scoring: %d sources scored, "
        "distribution=[mean=%.2f, min=%.2f, max=%.2f]",
        len(survivors),
        sum(s.scores.relevance for s in survivors) / len(survivors),
        min(s.scores.relevance for s in survivors),
        max(s.scores.relevance for s in survivors),
    )
    return survivors
