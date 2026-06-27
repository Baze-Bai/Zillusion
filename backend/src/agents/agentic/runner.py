"""Run the discovery agent end-to-end and convert its output to DataSource objects.

This is the glue layer between LangGraph and the Claude Agent SDK. The
LangGraph node ``agentic_discovery_node`` calls ``run_discovery_agent(state)``,
which:

1. Builds a prompt from ``state.requirement`` (the parsed user need).
2. Spins up an Agent SDK session pointed at DeepSeek's Anthropic-compatible
   endpoint, with the 10 tools from ``tools.py``.
3. Streams the agent loop, emitting per-tool-call progress for SSE.
4. Parses the agent's final JSON output into ``list[DataSource]`` (matching
   the shape that ``judge`` expects).
5. Calls ``flush_skills()`` defensively (the agent should have called it,
   but we don't trust LLM compliance for state persistence).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import traceback
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    create_sdk_mcp_server,
    query,
)

from src.agents.agentic.system_prompt import SYSTEM_PROMPT
from src.agents.agentic.tool_guard import build_workspace_confinement_hooks
from src.agents.agentic.tools import ALL_TOOLS, _get_session_dir
from src.config import settings
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.models.page_tree import DataPageNode, DataPageTree
from src.models.requirement import StructuredRequirement
from src.models.specs import APISpec, EmbeddedSpec, FileSpec
from src.services.run_logger import get_run_logger, log_event
from src.services.skill_library import get_skill_library
from src.tools.validation.url_canonicalizer import canonicalize_url as _canon_url

logger = logging.getLogger(__name__)


# Minimal backstop for the agent's MANDATORY FINAL STEP (reconcile
# task_description.md before final output). That prompt step has no tool gate,
# so a non-compliant agent can skip it. When the runner detects the description
# is stale relative to the committed results, it resumes the session ONCE with
# the nudge below. Flip to False to revert to pure-prompt behavior.
_FINAL_RECONCILE_NUDGE_ENABLED = True

_RECONCILE_NUDGE_PROMPT = (
    "Before this run is finalized: you committed data sources after you last "
    "updated task_description.md, so the description may no longer match what "
    "you actually did. Do the MANDATORY FINAL STEP now — Read task_description.md "
    "and correct any section that diverged from what you actually did this run "
    "(Goal / Discovery Strategy / Good enough; move any resolved assumption out "
    "of Open questions). Then re-emit your final JSON output exactly as "
    "specified. If after re-reading it is already accurate, re-emit the same "
    "final JSON unchanged."
)


# ──────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────


@dataclass
class AgentEvent:
    """A single SSE-eligible event emitted during the agent loop."""

    kind: str  # "agent_started" / "agent_text" / "tool_started" / "tool_completed" / "done" / "error"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    sources: list[DataSource]
    cost_usd: float
    duration_ms: float
    num_turns: int
    session_id: str | None
    raw_final_text: str
    exploration_notes: str = ""
    # Set ONLY when the run crashed (or ended with no result) — distinguishes a
    # genuine empty discovery from an infrastructure failure that returned []. The
    # consuming node raises on this instead of feeding [] into judge/finalize as a
    # clean "0 sources" success.
    error: str | None = None
    # DataPageTree objects produced by the agent for each portal it expanded.
    # Sources whose URL appears anywhere inside any tree are REMOVED from the
    # flat `sources` list (tree wins, see _apply_cross_structure_dedup).
    portal_trees: list[DataPageTree] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# Environment wiring (DeepSeek's Anthropic-compatible endpoint)
# ──────────────────────────────────────────────────────────────────────


def _deepseek_subprocess_env() -> dict[str, str]:
    """Build the env dict passed to the Claude Code CLI subprocess.

    We point the CLI at DeepSeek's Anthropic-compatible endpoint by
    reusing DEEPSEEK_API_KEY as ANTHROPIC_API_KEY. This is the same
    routing approach validated in backend/SPIKE.md.
    """
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not deepseek_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY missing — agentic runner needs it to authenticate "
            "the Anthropic-compatible endpoint."
        )
    return {
        "ANTHROPIC_API_KEY": deepseek_key,
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "zillusion-agentic/0.1",
        "CI": "1",  # don't prompt interactively
    }


# ──────────────────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────────────────


def _build_user_prompt(requirement: StructuredRequirement) -> str:
    """Render a StructuredRequirement into the discovery prompt.

    Composition:
      1. Structured requirement summary (the parsed intent fields).
      2. Per-query DYNAMIC ADDENDUM — runtime-computed directives that
         override the neutral system_prompt defaults when the query/state
         warrants it (e.g. explicit URL, scrape verb, no desired_formats).
      3. Final call-to-action.
    """
    lines = [
        "USER DATA NEED (structured by intent parser):",
        f"  original_query: {requirement.original_query}",
        f"  domain: {requirement.domain}",
    ]
    if requirement.sub_domains:
        lines.append(f"  sub_domains: {requirement.sub_domains}")
    if requirement.search_keywords_en:
        lines.append(f"  search_keywords_en: {requirement.search_keywords_en[:8]}")
    if getattr(requirement, "search_keywords_zh", None):
        lines.append(f"  search_keywords_zh: {requirement.search_keywords_zh[:5]}")
    if requirement.sub_questions:
        lines.append(f"  sub_questions: {requirement.sub_questions}")
    if getattr(requirement, "data_type_hints", None):
        lines.append(f"  data_type_hints: {requirement.data_type_hints}")
    if getattr(requirement, "desired_formats", None):
        lines.append(f"  desired_formats: {requirement.desired_formats}")
    if getattr(requirement, "geographic_scope", None):
        lines.append(f"  geographic_scope: {requirement.geographic_scope}")
    if getattr(requirement, "temporal_range", None):
        lines.append(f"  temporal_range: {requirement.temporal_range}")
    if getattr(requirement, "license_constraint", None):
        lines.append(f"  license_constraint: {requirement.license_constraint}")
    if getattr(requirement, "update_frequency", None):
        lines.append(f"  update_frequency: {requirement.update_frequency}")
    if getattr(requirement, "known_authoritative_sources", None):
        lines.append(f"  known_authoritative_sources: {requirement.known_authoritative_sources}")
    if getattr(requirement, "target_registries", None):
        lines.append(f"  target_registries (hints for query_registry worker_tag): "
                     f"{requirement.target_registries}")
    if getattr(requirement, "target_schema", None):
        lines.append(f"  target_schema: {str(requirement.target_schema)[:400]}")

    # ───── Per-query dynamic addendum (runtime-tuned overrides) ─────
    addendum = _build_prompt_addendum(requirement)
    if addendum:
        lines.append("")
        lines.append("=" * 70)
        lines.append("PER-QUERY DIRECTIVES (override system_prompt defaults)")
        lines.append("=" * 70)
        lines.append(addendum)

    lines.append("")
    lines.append("Discover usable data sources matching this need. Follow your system "
                 "prompt's process. Remember to call flush_skills() at the end and "
                 "produce the final JSON block exactly as specified.")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Per-query dynamic prompt addendum
# ──────────────────────────────────────────────────────────────────────


# URL extraction regex used for "did the user explicitly name a URL?" detection.
# Stops at whitespace / Chinese char / closing parens (which often follow URLs
# in human writing). Tolerates query-string ampersands and fragments.
_QUERY_URL_RE = re.compile(r"https?://[^\s一-鿿()()\[\]【】\"'<>]+")

# Verbs / phrases indicating the user wants to SCRAPE (vs use an API/dataset).
# Lowercased English + Chinese terms. New ones can be added as patterns emerge.
_SCRAPE_INTENT_TERMS = (
    "scrape", "scraping", "crawl ", "extract from website", "screen scrape",
    "抓取", "爬取", "爬虫", "扒下来", "扒取", "采集", "提取页面",
    "抓页面", "把网页", "从网页", "网页抓", "网站抓",
)


def _build_prompt_addendum(requirement: StructuredRequirement) -> str:
    """Compute a per-query addendum from the parsed requirement.

    The base agentic system_prompt is NEUTRAL about source types: APIs,
    datasets, listing pages, consumer search pages, official portals are
    all treated equally by default. This function injects targeted
    overrides ONLY when the user's literal query reveals a clear bias —
    e.g. they named a specific URL, used scrape verbs, or skipped
    desired_formats entirely (signalling open-ended discovery).

    Add new signals here as the project encounters new patterns in
    production — this is the explicit "fine-tune system_prompt at
    runtime" hook (per project requirement).
    """
    parts: list[str] = []
    q = (getattr(requirement, "original_query", "") or "")
    q_lower = q.lower()

    # Signal 1: explicit URL(s) in the user's query
    urls = _QUERY_URL_RE.findall(q)
    if urls:
        bullet_list = "\n".join(f"    - {u}" for u in urls)
        parts.append(
            "▸ PRIORITY URL OVERRIDE\n"
            f"  The user explicitly named the following URL(s) in their query:\n"
            f"{bullet_list}\n"
            "  Process them DIRECTLY first via fetch_page or crawl_list_tree,\n"
            "  BEFORE doing broad search_web exploration. Treat them as the\n"
            "  PRIMARY targets; search_web is purely supplementary in this run."
        )

    # Signal 2: scrape / crawl intent verbs
    if any(t in q_lower for t in _SCRAPE_INTENT_TERMS):
        parts.append(
            "▸ SCRAPING-MODE OVERRIDE\n"
            "  The user explicitly asked for SCRAPING / page extraction.\n"
            "  Do NOT bias search_web queries toward APIs or datasets. Treat\n"
            '  commercial listing pages, consumer search-result pages, and\n'
            '  login-walled SPAs as EQUALLY VALID `"embedded"` candidates.\n'
            "  Prefer `crawl_list_tree` over `query_registry` for this run."
        )

    # Signal 3: no explicit format → fully neutral discovery
    formats = getattr(requirement, "desired_formats", None) or []
    if not formats:
        parts.append(
            "▸ NEUTRAL DISCOVERY MODE\n"
            "  The user did NOT specify a preferred format. APIs, datasets,\n"
            "  official portals, consumer listing pages, and search-result\n"
            "  pages are ALL valid candidates. Do NOT pre-filter by source\n"
            "  type. Keep search_web queries NEUTRAL — avoid auto-injecting\n"
            "  qualifiers like 'API', 'developer', 'open platform', 'dataset'\n"
            "  unless they genuinely fit the user's literal need."
        )

    return "\n\n".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Final-output parsing
# ──────────────────────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Extract the first JSON code block from the agent's final message."""
    if not text:
        return None
    # Try fenced ```json ... ``` block first
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            logger.warning("agentic.parse_json fence block failed: %s", e)

    # Try whole message as JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Last resort: regex for the outermost { ... } that contains "sources"
    brace_match = re.search(r'\{[^{}]*"sources"\s*:\s*\[.*?\]\s*[^{}]*\}', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("agentic.parse_json: no JSON object with 'sources' found in final text")
    return None


def _coerce_source_type(raw: Any) -> DataSourceType:
    key = str(raw or "").strip().lower()
    name_map = {
        "api": DataSourceType.API,
        "file": DataSourceType.DOWNLOADABLE_FILE,
        "downloadable_file": DataSourceType.DOWNLOADABLE_FILE,
        "embedded": DataSourceType.EMBEDDED_DATA,
        "embedded_data": DataSourceType.EMBEDDED_DATA,
    }
    return name_map.get(key, DataSourceType.EMBEDDED_DATA)


def _coerce_source_types(raw: Any) -> list[DataSourceType]:
    """Accept a single category name ("embedded") OR a list (["embedded","file"])
    → deduped list of DataSourceType, order preserved. A source can be MULTIPLE
    categories (e.g. a dataset page with a Download CSV button = embedded + file);
    each becomes its own single-type DataSource downstream. Empty/garbage →
    [EMBEDDED_DATA] (safe default)."""
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[DataSourceType] = []
    for it in items:
        if it is None or (isinstance(it, str) and not it.strip()):
            continue
        t = _coerce_source_type(it)
        if t not in out:
            out.append(t)
    return out or [DataSourceType.EMBEDDED_DATA]


def _coerce_access_level(raw: Any) -> AccessLevel:
    key = str(raw or "").strip().lower()
    name_map = {
        "open": AccessLevel.OPEN,
        "free_reg": AccessLevel.FREE_REGISTRATION,
        "free_registration": AccessLevel.FREE_REGISTRATION,
        "api_key_free": AccessLevel.API_KEY_FREE,
        "api_key_paid": AccessLevel.API_KEY_PAID,
        "oauth": AccessLevel.OAUTH,
        "paywall": AccessLevel.PAYWALL,
        "unknown": AccessLevel.UNKNOWN,
    }
    return name_map.get(key, AccessLevel.UNKNOWN)


def _build_data_sources(src_dict: dict[str, Any]) -> list[DataSource]:
    """Convert one LLM-emitted source dict into ONE DataSource PER category.

    ``source_type`` may be a single category name ("embedded") or a list
    (["embedded","file"]) when a source offers multiple kinds of data. Each
    category becomes its own single-type DataSource (id = hash(url+type), so
    same-url products stay distinct) — so each is judged independently and
    turned into its own workflow downstream (one product → one site). Returns
    [] on validation failure (no source emitted)."""
    import hashlib

    url = (src_dict.get("url") or "").strip()
    if not url:
        return []

    types = _coerce_source_types(src_dict.get("source_type"))
    name = (src_dict.get("name") or url).strip()[:200]
    description = (src_dict.get("description") or "").strip()
    if not description:
        description = f"{name} — discovered by agentic pipeline"

    # Fields shared across every category of this source.
    shared: dict[str, Any] = dict(
        name=name,
        provider=src_dict.get("provider") or "",
        url=url,
        description=description[:1000],
        domain=src_dict.get("domain") or "general",
        tags=src_dict.get("tags") or [],
        data_format=src_dict.get("data_format") or [],
        geographic_coverage=src_dict.get("geographic_coverage") or [],
        temporal_coverage=src_dict.get("temporal_coverage"),
        update_frequency=src_dict.get("update_frequency"),
        access_level=_coerce_access_level(src_dict.get("access_level")),
        license=src_dict.get("license"),
        pricing=src_dict.get("pricing"),
        rate_limit=src_dict.get("rate_limit"),
        scores=None,
        discovery_method=src_dict.get("discovery_method") or "agentic",
        discovered_at=datetime.utcnow(),
        metadata=src_dict.get("metadata") or {},
    )

    out: list[DataSource] = []
    for source_type in types:
        source_id = hashlib.sha256(f"{url}:{source_type.value}".encode()).hexdigest()[:16]
        # Type-specific spec — minimal stub so judge has something to look at.
        api_spec = file_spec = embedded_spec = None
        if source_type == DataSourceType.API:
            # Read the FULL api spec the agent emitted. Previously only
            # endpoint/auth_type/docs were read, so signup_url / auth_location /
            # openapi etc. the agent discovered were silently dropped here — which
            # broke the whole "discovery must surface how to get a key" path
            # (the key-gated API agent + finalize's quickstart both read these).
            # Agent omits a field → None/default, identical to the old behavior.
            api_spec = APISpec(
                endpoint=src_dict.get("api_endpoint") or url,
                method=src_dict.get("api_method") or "GET",
                auth_type=src_dict.get("auth_type", "unknown"),
                auth_location=src_dict.get("auth_location"),
                auth_param_name=src_dict.get("auth_param_name"),
                signup_url=src_dict.get("signup_url"),
                signup_instructions=src_dict.get("signup_instructions"),
                openapi_spec_url=src_dict.get("openapi_spec_url"),
                has_sdk=bool(src_dict.get("has_sdk", False)),
                example_code=src_dict.get("example_code"),
                documentation_url=src_dict.get("docs_url") or url,
            )
        elif source_type == DataSourceType.DOWNLOADABLE_FILE:
            file_spec = FileSpec(
                download_url=src_dict.get("download_url") or url,
                file_format=(src_dict.get("data_format") or ["unknown"])[0]
                if isinstance(src_dict.get("data_format"), list)
                else (src_dict.get("file_format") or "unknown"),
            )
        elif source_type == DataSourceType.EMBEDDED_DATA:
            meta = src_dict.get("metadata") or {}
            embedded_spec = EmbeddedSpec(
                extraction_method=meta.get("extracted_via") or "agentic_inferred",
                data_shape=meta.get("data_shape", "unknown"),
                fields_present=meta.get("fields_present") or [],
                coverage=float(meta.get("coverage", 0.5)),
                extraction_difficulty=meta.get("extraction_difficulty", "medium"),
            )
        try:
            out.append(DataSource(
                id=source_id,
                source_type=source_type,
                api_spec=api_spec,
                file_spec=file_spec,
                embedded_spec=embedded_spec,
                **shared,
            ))
        except Exception as e:
            logger.warning("agentic.build_source failed for %s [%s]: %s", url[:80], source_type.value, e)
    return out


# ──────────────────────────────────────────────────────────────────────
# Portal tree parsing + cross-structure dedup
# ──────────────────────────────────────────────────────────────────────


def _safe_canonical(url: str) -> str:
    """Best-effort canonicalize; falls back to lowercase + strip slash."""
    try:
        return _canon_url(url)
    except Exception:
        return (url or "").lower().rstrip("/")


def _walk_tree_urls(node: DataPageNode, accumulator: set[str]) -> None:
    """Collect every canonicalized URL in this node and all descendants."""
    if node.url:
        accumulator.add(_safe_canonical(node.url))
    for child in node.children or []:
        _walk_tree_urls(child, accumulator)


def _load_portal_tree_evidence() -> tuple[dict[str, dict], dict[str, dict]]:
    """Parse the active run log JSONL to build two evidence indexes used by
    portal_tree validation:

      - ``by_session``: ``{session_id: tool_result_summary}`` for crawl_list_tree
        calls. This is the canonical anchor — when the agent puts a
        ``crawl_session_id`` on a DataPageTree, we look it up here.
      - ``by_url``: ``{canonical_url: tool_result_summary}`` indexing
        crawl_list_tree AND firecrawl_map calls. Used as a forgiving fallback
        when the agent omits ``crawl_session_id`` so we can still tell apart
        "agent forgot the field but did call the tool" from "agent never
        called the tool at all" (Agoda / Trip.com pattern from hotel-5bdb42).

    Returns empty dicts if no run logger is active or the log can't be read
    — validation downstream then degrades to no-evidence mode (no warnings
    emitted, behaviour identical to pre-Fix-B).
    """
    rl = get_run_logger()
    if rl is None or not rl.path.exists():
        return {}, {}

    by_session: dict[str, dict] = {}
    by_url: dict[str, dict] = {}
    try:
        with rl.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") != "tool_result":
                    continue
                data = rec.get("data") or {}
                tool = data.get("tool")
                summary = data.get("summary") or {}
                if tool == "crawl_list_tree":
                    # Legacy single-URL shape
                    sid = summary.get("session_id")
                    if sid:
                        by_session[sid] = summary
                    seed = summary.get("seed_url")
                    if seed:
                        by_url[_safe_canonical(seed)] = summary
                    # New parallel shape — index each per_url_summary entry
                    for per in (summary.get("per_url_summary") or []):
                        if not isinstance(per, dict):
                            continue
                        psid = per.get("session_id")
                        if psid:
                            by_session[psid] = per
                        pseed = per.get("seed_url")
                        if pseed:
                            by_url[_safe_canonical(pseed)] = per
                elif tool == "firecrawl_map":
                    # firecrawl_map gets its own outer session_id (fcmap-XXXX)
                    # since Fix A — the agent can use it as crawl_session_id
                    # on portal_trees emitted from the sitemap-only path
                    # (no fallback). When fallback ran, the inner crawl_list_
                    # tree call ALSO appears as its own tool_result above and
                    # its session_id (fcmap-fb-XXX) goes into by_session via
                    # that path; the agent is free to use either anchor.
                    # Legacy single-URL shape
                    sid = summary.get("session_id")
                    if sid:
                        by_session[sid] = summary
                    map_url = summary.get("url")
                    if map_url:
                        by_url[_safe_canonical(map_url)] = summary
                    # New parallel shape — index each per_url_summary entry
                    # (both outer fcmap-XXX session_id and inner fcmap-fb-XXX
                    # fallback session_id show up here per URL).
                    for per in (summary.get("per_url_summary") or []):
                        if not isinstance(per, dict):
                            continue
                        psid = per.get("session_id")
                        if psid:
                            by_session[psid] = per
                        fb_sid = per.get("fallback_session_id")
                        if fb_sid:
                            by_session[fb_sid] = per
                        purl = per.get("url")
                        if purl:
                            by_url[_safe_canonical(purl)] = per
    except Exception as e:
        logger.warning(
            "agentic.runner: failed reading run log %s for portal_tree "
            "validation: %s — validation degrades to no-evidence mode",
            rl.path, e,
        )
        return {}, {}

    return by_session, by_url


# Phrases that, when present in tree_summary, hard-contradict a tool result
# with leaf>0. Lower-cased; substring match. Pulled from the actual hotel-
# 5bdb42 Booking narrative ("zero hotel data extractable") plus Chinese
# equivalents and common variations. These are unambiguous — any of them
# appearing as a substring means the agent is asserting nothing was found.
_ZERO_DATA_PHRASES = (
    "zero data", "zero hotel", "zero records", "zero results",
    "no data", "no records", "no hotel", "no extractable",
    "nothing extractable", "completely missing", "completely empty",
    "empty results",
    "无数据", "零数据", "没有数据", "无任何数据",
)


# Digit-prefixed "zero" phrases need a word boundary check — otherwise
# count phrases like "4,550 hotels" or "1,600 records" match "0 hotels"
# as a substring, generating a false-positive B1 contradiction. Seen in
# hotel-5c run: Booking tree_summary mentioned "$71, 4,550 hotels" which
# triggered B1 even though the narrative was honest.
#
# (?<![\d,.]) ensures the matched 0 is NOT preceded by digit, comma, or
# decimal point — so "1,550 hotels" / "20 hotels" / "9.0 hotels" all skip
# but "0 hotels" / " 0 hotels" / "found 0 hotels" / "$0 hotels" trigger.
_ZERO_DIGIT_PATTERNS = (
    re.compile(r"(?<![\d,.])0\s+hotels?\b", re.IGNORECASE),
    re.compile(r"(?<![\d,.])0\s+records?\b", re.IGNORECASE),
    re.compile(r"(?<![\d,.])0\s+results?\b", re.IGNORECASE),
)


def _zero_data_phrase_in(text_lower: str) -> str | None:
    """Return the first zero-data phrase that hits ``text_lower``, or None.

    Combines substring matching (unambiguous phrases) with word-bounded
    regex matching (digit-prefixed phrases that risk false positives in
    count contexts).
    """
    for phrase in _ZERO_DATA_PHRASES:
        if phrase in text_lower:
            return phrase
    for pat in _ZERO_DIGIT_PATTERNS:
        m = pat.search(text_lower)
        if m:
            return m.group(0)
    return None


# Loose digit-presence check for the numbers-quoting rule. We can't verify
# WHICH numbers the agent quoted, but the absence of ANY digits when the
# tool returned a non-trivial visit count is a strong tell.
_DIGIT_RE = re.compile(r"\d{1,5}")


def _validate_portal_tree_evidence(
    tree: DataPageTree,
    by_session: dict[str, dict],
    by_url: dict[str, dict],
) -> list[str]:
    """Warn-only validation: does the tree's narrative line up with the
    crawl_list_tree / firecrawl_map result actually in the run log?

    Returns a list of warning strings — empty means clean. Caller attaches
    these to ``tree.validation_warnings`` and never drops on this basis
    (warn-only release; flip to drop after a week of data).

    Checks:
      E.1  missing ``crawl_session_id`` AND no tool call recorded for this
           URL  ⇒  agent skipped the mandatory tool (Agoda / Trip.com
           pattern in hotel-5bdb42)
      E.2  ``crawl_session_id`` provided but not in run log  ⇒  fabricated
           anchor
      E.3  ``crawl_session_id`` provided but its tool record is for a
           DIFFERENT URL  ⇒  mismatched anchor (likely copy-paste from a
           previous unrelated call)
      B.1  tree_summary contains a "zero / no / empty data" phrase, but
           the tool's ``page_kind_counts.leaf`` > 0  ⇒  HARD CONTRADICTION
           (Booking 8-leaves vs "zero hotel data" pattern)
      D    tool visited > 1 page but tree_summary has no digits at all
           ⇒  numbers-quoting rule violation (agent didn't cite evidence)
    """
    warnings_out: list[str] = []
    csi = (tree.crawl_session_id or "").strip()
    tree_url = _safe_canonical(tree.root.url) if (tree.root and tree.root.url) else ""

    summary: dict | None = None

    if not csi:
        # E.1 — Did the agent call ANY discovery tool for this URL?
        if not tree_url or tree_url not in by_url:
            warnings_out.append(
                f"E1_no_call: crawl_session_id missing AND no crawl_list_tree / "
                f"firecrawl_map call found for url={tree.root.url!r} — agent "
                f"skipped the mandatory tool before emitting this portal_tree"
            )
            return warnings_out
        # URL has SOME tool call — recoverable, but agent should have anchored
        warnings_out.append(
            f"E1_csi_missing_url_match: crawl_session_id missing; recovered "
            f"via URL match (tool was called, agent forgot to anchor)"
        )
        summary = by_url[tree_url]
    else:
        # E.2 — session_id provided; must exist in log
        if csi not in by_session:
            warnings_out.append(
                f"E2_fabricated_csi: crawl_session_id={csi!r} not found in run "
                f"log — agent invented the evidence anchor"
            )
            return warnings_out
        summary = by_session[csi]
        # E.3 — session_id real, but anchored to a different URL.
        # crawl_list_tree summary stores it under `seed_url`; firecrawl_map
        # under `url`. Accept either so the check works for both tools.
        tool_url = _safe_canonical(
            summary.get("seed_url") or summary.get("url") or ""
        )
        if tree_url and tool_url and tree_url != tool_url:
            warnings_out.append(
                f"E3_csi_url_mismatch: crawl_session_id={csi!r} was for "
                f"url={tool_url!r}, but tree.root.url={tree_url!r}"
            )

    if summary is None:
        return warnings_out

    # B.1 — zero-data narrative vs leaf>0
    kind_counts = summary.get("page_kind_counts") or {}
    try:
        leaf_n = int(kind_counts.get("leaf") or 0)
    except (TypeError, ValueError):
        leaf_n = 0
    tree_summary_lower = (tree.tree_summary or "").lower()
    if leaf_n > 0:
        hit = _zero_data_phrase_in(tree_summary_lower)
        if hit is not None:
            warnings_out.append(
                f"B1_contradiction: tree_summary contains {hit!r} but "
                f"tool found page_kind_counts.leaf={leaf_n}, "
                f"total_pages_visited={summary.get('total_pages_visited')}"
            )

    # D — numbers-quoting heuristic
    try:
        total_visited = int(summary.get("total_pages_visited") or 0)
    except (TypeError, ValueError):
        total_visited = 0
    if total_visited > 1 and not _DIGIT_RE.search(tree.tree_summary or ""):
        warnings_out.append(
            f"D_no_digits: tool visited {total_visited} pages but tree_summary "
            f"contains no digits at all — evidence not cited"
        )

    return warnings_out


_AGENT_SESSIONS_ROOT = Path("agent-workspace/agent-sessions")


def _get_committed_session_dir() -> Path | None:
    """Mirror of tools._get_session_dir — same path derivation, but runner
    can return None when no run log is bound (the commit tools also no-op
    in that case)."""
    rl = get_run_logger()
    qid = (getattr(rl, "query_id", None) or "").strip()
    if not qid:
        return None
    qid = qid.replace("/", "_").replace("\\", "_").replace("..", "_")[:64]
    sd = _AGENT_SESSIONS_ROOT / qid
    return sd if sd.exists() else None


def _load_committed_from_jsonl(
    session_dir: Path,
) -> tuple[list[dict], list[dict], set[str], dict[str, int]]:
    """Read portal_trees.jsonl + sources.jsonl from a committed session
    directory. Returns:

      (tree_dicts, source_dicts, tombstoned_canonicals, stats)

    where ``tree_dicts`` / ``source_dicts`` are the agent-committed raw
    dicts ready to feed into ``_build_portal_trees`` / ``_build_data_
    source``. Tombstones in sources.jsonl are honored — tombstoned URLs
    are excluded from ``source_dicts`` and surfaced in
    ``tombstoned_canonicals`` for later cross-checking.

    Empty lists (no commits) are NOT an error — caller decides whether
    to fall back to parsing the agent's final JSON text.
    """
    stats = {
        "trees_committed": 0, "sources_committed_raw": 0,
        "sources_tombstoned": 0, "sources_after_tombstone": 0,
    }

    trees_path = session_dir / "portal_trees.jsonl"
    sources_path = session_dir / "sources.jsonl"

    tree_dicts: list[dict] = []
    if trees_path.exists():
        try:
            for line in trees_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        tree_dicts.append(rec)
                except Exception:
                    continue
        except Exception as e:
            logger.warning("session portal_trees.jsonl read failed: %s", e)
    stats["trees_committed"] = len(tree_dicts)

    # Two-pass over sources: first collect tombstones, then live records
    tombstoned: set[str] = set()
    raw_records: list[dict] = []
    if sources_path.exists():
        try:
            for line in sources_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("_tombstone"):
                    t_url = rec.get("tombstoned_url") or ""
                    if t_url:
                        tombstoned.add(_safe_canonical(t_url))
                    stats["sources_tombstoned"] += 1
                else:
                    raw_records.append(rec)
        except Exception as e:
            logger.warning("session sources.jsonl read failed: %s", e)
    stats["sources_committed_raw"] = len(raw_records)

    # Filter raw records by tombstones
    source_dicts: list[dict] = []
    for rec in raw_records:
        canon = _safe_canonical(rec.get("url", ""))
        if canon and canon in tombstoned:
            continue
        source_dicts.append(rec)
    stats["sources_after_tombstone"] = len(source_dicts)

    return tree_dicts, source_dicts, tombstoned, stats


def _merge_committed_and_final_json(
    committed_trees: list[dict],
    committed_sources: list[dict],
    final_json_trees: list[dict],
    final_json_sources: list[dict],
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Dual-track merge: committed JSONL is preferred source of truth; the
    agent's final-text JSON is supplementary fallback for items that the
    agent forgot to commit (or for development phase before agents are
    100% emit-as-you-go compliant).

    Merge rules:
      - Trees: keyed by canonical root URL. Committed version wins on
        conflict; final-JSON-only trees are appended.
      - Sources: keyed by canonical URL. Committed wins. Final-JSON-only
        sources are appended PROVIDED their URL isn't already covered by
        any merged tree (cross-structure dedup happens later in the
        existing _apply_cross_structure_dedup pass, this just stops the
        obvious duplication).

    Returns (merged_trees, merged_sources, merge_stats).
    """
    merge_stats: dict[str, int] = {
        "trees_committed": len(committed_trees),
        "trees_from_final_json": len(final_json_trees),
        "trees_merged": 0,
        "trees_final_json_supplemented": 0,
        "sources_committed": len(committed_sources),
        "sources_from_final_json": len(final_json_sources),
        "sources_merged": 0,
        "sources_final_json_supplemented": 0,
    }

    # ── Merge trees ──
    merged_trees: list[dict] = list(committed_trees)
    committed_root_urls = {
        _safe_canonical(((t.get("root") or {}).get("url")) or "")
        for t in committed_trees
    }
    committed_root_urls.discard("")
    for ft in final_json_trees:
        if not isinstance(ft, dict):
            continue
        ft_root_url = _safe_canonical(((ft.get("root") or {}).get("url")) or "")
        if ft_root_url and ft_root_url in committed_root_urls:
            continue  # committed version takes precedence
        merged_trees.append(ft)
        merge_stats["trees_final_json_supplemented"] += 1
    merge_stats["trees_merged"] = len(merged_trees)

    # ── Merge sources ──
    merged_sources: list[dict] = list(committed_sources)
    committed_source_urls = {
        _safe_canonical(s.get("url", "")) for s in committed_sources
    }
    committed_source_urls.discard("")
    for fs in final_json_sources:
        if not isinstance(fs, dict):
            continue
        fs_url = _safe_canonical(fs.get("url", ""))
        if fs_url and fs_url in committed_source_urls:
            continue
        merged_sources.append(fs)
        merge_stats["sources_final_json_supplemented"] += 1
    merge_stats["sources_merged"] = len(merged_sources)

    return merged_trees, merged_sources, merge_stats


def _build_portal_trees(
    tree_dicts: list[Any],
) -> tuple[list[DataPageTree], set[str], dict[str, int]]:
    """Parse the agent's `portal_trees` JSON blocks into DataPageTree objects.

    Pydantic-malformed trees are still dropped (the JSON contract is the
    final wall). But evidence-mismatches — missing crawl_session_id,
    fabricated session_id, narrative contradicting tool output — are NOT
    drop-worthy in warn-only mode; they're recorded in
    ``tree.validation_warnings`` and counted into the returned stats so the
    diagnostics_writeback node can surface them per-run.

    Returns:
      (validated_trees, set_of_canonical_urls_anywhere_in_any_tree,
       validation_counter_dict)

    The counter dict shape is ``{warning_code: count}`` — keys are the
    ``E1_*`` / ``E2_*`` / ``E3_*`` / ``B1_*`` / ``D_*`` codes emitted by
    ``_validate_portal_tree_evidence``, plus ``total_warnings`` and
    ``trees_with_warnings``.
    """
    trees: list[DataPageTree] = []
    all_tree_urls: set[str] = set()

    # Build evidence indexes ONCE for the whole batch — reading the JSONL
    # repeatedly per tree would be O(N×log_size). One read covers everything.
    by_session, by_url = _load_portal_tree_evidence()

    counter: dict[str, int] = {"total_warnings": 0, "trees_with_warnings": 0}

    for i, td in enumerate(tree_dicts):
        if not isinstance(td, dict):
            logger.warning("agentic.runner: portal_trees[%d] is not a dict; skipping", i)
            continue
        try:
            tree = DataPageTree.model_validate(td)
        except Exception as exc:
            logger.warning(
                "agentic.runner: portal_trees[%d] failed Pydantic validation: %s; "
                "skipping. raw keys=%s",
                i, exc, list(td.keys()),
            )
            continue

        # Warn-only evidence validation. Tree is included regardless of result.
        warnings_for_tree = _validate_portal_tree_evidence(tree, by_session, by_url)
        if warnings_for_tree:
            tree.validation_warnings = warnings_for_tree
            counter["trees_with_warnings"] += 1
            counter["total_warnings"] += len(warnings_for_tree)
            for w in warnings_for_tree:
                code = w.split(":", 1)[0]  # e.g. "E1_no_call"
                counter[code] = counter.get(code, 0) + 1
            logger.warning(
                "agentic.runner: portal_trees[%d] url=%s evidence-validation "
                "flagged %d warning(s): %s",
                i, tree.root.url, len(warnings_for_tree),
                "; ".join(warnings_for_tree),
            )

        trees.append(tree)
        _walk_tree_urls(tree.root, all_tree_urls)

    if trees:
        logger.info(
            "agentic.runner: parsed %d portal_trees with %d total URLs inside "
            "(%d trees flagged with %d total validation warnings)",
            len(trees), len(all_tree_urls),
            counter["trees_with_warnings"], counter["total_warnings"],
        )
    return trees, all_tree_urls, counter


def _apply_cross_structure_dedup(
    sources: list[DataSource],
    tree_urls: set[str],
) -> tuple[list[DataSource], dict[str, int]]:
    """Two-pass dedup:
      1. Drop any flat source whose canonical URL is already inside a tree.
      2. Drop intra-list canonical duplicates (keep first).

    Returns (deduped_sources, stats_dict).
    """
    stats = {"input": len(sources), "dropped_in_tree": 0, "dropped_duplicate": 0}
    seen: set[str] = set()
    out: list[DataSource] = []
    for ds in sources:
        canon = _safe_canonical(ds.url)
        if not canon:
            continue
        if canon in tree_urls:
            stats["dropped_in_tree"] += 1
            continue
        if canon in seen:
            stats["dropped_duplicate"] += 1
            continue
        seen.add(canon)
        out.append(ds)
    stats["output"] = len(out)
    return out, stats


# ──────────────────────────────────────────────────────────────────────
# Message → event translation
# ──────────────────────────────────────────────────────────────────────


_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "natural_end",
    "stop_sequence": "natural_end",
    "max_tokens": "budget_exceeded",
    "tool_use": "needs_tool",
    "tool_use_limit": "budget_exceeded",
    "max_turns_reached": "budget_exceeded",
    "max_turns": "budget_exceeded",
    "error": "error",
    "refusal": "refusal",
    "pause_turn": "interrupted",
    "interrupted": "interrupted",
}


def _normalize_stop_reason(raw: str) -> str:
    """Map SDK/provider-specific stop_reason strings to a stable enum.

    Enum values: natural_end | needs_tool | budget_exceeded | error | refusal
    | interrupted | unknown. Stable across provider variants so cross-run
    analytics don't have to track every SDK string verbatim.
    """
    if not raw:
        return "unknown"
    key = str(raw).strip().lower()
    return _STOP_REASON_MAP.get(key, "unknown")


def _jsonable_usage(usage: Any) -> Any:
    """Coerce an SDK usage object/dict into a small JSON-able token-count dict."""
    if usage is None:
        return None
    src = usage if isinstance(usage, dict) else (getattr(usage, "__dict__", None) or {})
    if isinstance(src, dict):
        keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens")
        out = {k: src[k] for k in keys if src.get(k) is not None}
        if out:
            return out
    return usage if isinstance(usage, dict) else str(usage)


def _jsonable_model_usage(mu: Any) -> Any:
    """ResultMessage.model_usage is {model -> usage}; coerce each value."""
    if mu is None:
        return None
    if isinstance(mu, dict):
        return {str(k): _jsonable_usage(v) for k, v in mu.items()}
    return str(mu)


def _log_agent_turn(msg: Any, turn_seq: int) -> None:
    """Per-turn visibility into the agentic SDK loop: which model served this
    turn (primary vs fallback) + per-turn token usage. Without this the loop is
    a near-black-box — agent_done only carries aggregate cost/turns, so a slow
    or expensive turn, or a silent model fallback, was undiagnosable.
    """
    try:
        log_event("agent_turn", {
            "turn_seq": turn_seq,
            "model": getattr(msg, "model", None),
            "stop_reason": getattr(msg, "stop_reason", None),
            "usage": _jsonable_usage(getattr(msg, "usage", None)),
        })
    except Exception:  # logging must NEVER break the agent loop
        pass


def _message_to_events(msg: Any, turn_seq: int) -> list[AgentEvent]:
    """Convert one SDK message into 0..N SSE-eligible events.

    ``turn_seq`` is the index of the assistant turn this message belongs to
    (incremented by the caller when an AssistantMessage arrives). Stamped
    onto every emitted event so agent_text and agent_tool_use blocks from
    the same turn can be re-joined downstream.
    """
    out: list[AgentEvent] = []

    if isinstance(msg, SystemMessage):
        subtype = getattr(msg, "subtype", "")
        if subtype == "init":
            data = getattr(msg, "data", {}) or {}
            out.append(AgentEvent("agent_started", {
                "session_id": data.get("session_id"),
                "cwd": data.get("cwd"),
            }))
        return out

    if isinstance(msg, AssistantMessage):
        for block in (getattr(msg, "content", None) or []):
            btype = getattr(block, "type", "") or type(block).__name__
            if btype == "TextBlock" or hasattr(block, "text"):
                text = (getattr(block, "text", "") or "").strip()
                if text:
                    # AgentEvent for SSE keeps the 500-char preview (UI
                    # rendering budget); the structured run log gets the
                    # FULL text so the "complete trace" requirement holds
                    # — without this the JSON sources block at end-of-run
                    # is truncated mid-record.
                    out.append(AgentEvent(
                        "agent_text",
                        {"text": text[:500], "text_full": text, "turn_seq": turn_seq},
                    ))
            elif btype == "ToolUseBlock" or hasattr(block, "name"):
                tool_name = getattr(block, "name", "?")
                tool_id = getattr(block, "id", None)
                tool_input = getattr(block, "input", "")
                # SSE event (consumed by SSE bridge / graph node)
                out.append(AgentEvent("tool_started", {
                    "tool": tool_name,
                    "tool_use_id": tool_id,
                    "input_preview": str(tool_input)[:300],
                    "turn_seq": turn_seq,
                }))
                # Also log to run log so built-in tools (Read/Grep/Glob/
                # Write/Edit/Bash) show up — MCP tools self-log via
                # _log_tool_call inside their handler, but built-in
                # tools are intercepted by claude CLI directly and never
                # reach our handler code. Without this we have no
                # visibility into when the agent actually uses Read etc.
                log_event("agent_tool_use", {
                    "tool": tool_name,
                    "tool_use_id": tool_id,
                    "input": tool_input if isinstance(tool_input, dict) else str(tool_input)[:1000],
                    "turn_seq": turn_seq,
                })
                # The agent talking TO the user (send_user_message tool) →
                # surface a PERSISTED chat message from the FULL, untruncated
                # tool input. We read it off the ToolUseBlock here (the stream)
                # rather than from the in-process handler, mirroring how
                # task_description / agent_text reach the UI.
                if tool_name.endswith("__send_user_message") or tool_name == "send_user_message":
                    msg_text = (
                        str(tool_input.get("message") or "").strip()
                        if isinstance(tool_input, dict)
                        else ""
                    )
                    if msg_text:
                        out.append(AgentEvent("agent_message", {
                            "text": msg_text,
                            "turn_seq": turn_seq,
                            # Stamped so the UI can tell a just-arrived message
                            # (typewriter reveal) from replayed history.
                            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        }))
        return out

    if isinstance(msg, UserMessage):
        for block in (getattr(msg, "content", None) or []):
            if hasattr(block, "tool_use_id"):
                preview = str(getattr(block, "content", ""))[:300]
                out.append(AgentEvent("tool_completed", {
                    "tool_use_id": getattr(block, "tool_use_id", None),
                    "result_preview": preview,
                    "turn_seq": turn_seq,
                }))
        return out

    if isinstance(msg, ResultMessage):
        raw_stop = getattr(msg, "stop_reason", "")
        out.append(AgentEvent("done", {
            "result": getattr(msg, "result", ""),
            "cost_usd": getattr(msg, "total_cost_usd", 0.0),
            "duration_ms": getattr(msg, "duration_ms", 0),
            "num_turns": getattr(msg, "num_turns", 0),
            "stop_reason": raw_stop,
            "stop_reason_normalized": _normalize_stop_reason(raw_stop),
            "is_error": getattr(msg, "is_error", False),
            # Per-model token breakdown across the whole agent loop — the only
            # place that reveals which model(s) actually served the agentic
            # turns (e.g. primary vs fallback) and at what token volume.
            "model_usage": _jsonable_model_usage(getattr(msg, "model_usage", None)),
        }))
    return out


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────


async def stream_discovery_agent(
    requirement: StructuredRequirement,
    *,
    workspace_dir: Path | None = None,
    max_turns: int = 100,
    model: str = "deepseek-v4-pro",
    fallback_model: str = "deepseek-v4-flash",
    steer_queue: asyncio.Queue | None = None,
) -> AsyncIterator[AgentEvent | AgentRunResult]:
    """Async generator yielding AgentEvent for each step, then a final AgentRunResult.

    The caller (LangGraph node or SSE bridge) can stream events to the user
    in real time. The last yielded value is always an AgentRunResult.

    ``steer_queue`` (optional) enables TURN-LEVEL live steering: when provided,
    the prompt is sent as an SDK streaming-input async-iterable instead of a
    one-shot string, so messages pushed onto the queue mid-run are delivered to
    the live agent (it incorporates them on its next turn). The queue is closed
    with a ``None`` sentinel automatically when the agent finishes. When
    ``steer_queue is None`` (default) behavior is the unchanged one-shot path.
    """
    user_prompt = _build_user_prompt(requirement)

    # Align the SDK cwd with the emit-as-you-go session dir
    # (`agent-workspace/agent-sessions/<qid>/`). This way the agent can use
    # the built-in Read/Grep/Glob tools to inspect files written by our
    # tools (fetch_page -> fetched/<hash>.md, commit_* -> *.jsonl) with
    # simple relative paths, and the system_prompt.md scratch file lives
    # alongside the run's artifacts for easy auditing.
    #
    # MUST be absolute: SDK subprocess uses str(workspace_dir) as cwd, and
    # claude CLI then resolves --system-prompt-file PATH inside that cwd.
    # If workspace_dir is relative, the path gets double-nested into the
    # subprocess cwd. _get_session_dir() returns a project-relative Path,
    # so we always .resolve() before passing to the SDK.
    workspace_dir = (workspace_dir or _get_session_dir()).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    tools_server = create_sdk_mcp_server(
        name="zillusion-discovery",
        version="0.1.0",
        tools=ALL_TOOLS,
    )

    # MCP tool names are namespaced: mcp__<server_name>__<tool_name>
    namespaced_tools = [f"mcp__zillusion-discovery__{t.name}" for t in ALL_TOOLS]

    # Snapshot the actual tool surface exposed to this agent run so cross-run
    # behavior diffs can ask "did the tool list change between A and B?".
    # Includes both MCP (zillusion-discovery server) and SDK built-ins.
    log_event("mcp_tools_registered", {
        "mcp_server_name": "zillusion-discovery",
        "mcp_server_version": "0.1.0",
        "mcp_tools": [t.name for t in ALL_TOOLS],
        "mcp_tools_count": len(ALL_TOOLS),
        "builtin_tools": ["Read", "Write", "Edit", "Grep", "Glob"],
        "model": model,
        "fallback_model": fallback_model,
        "max_turns": max_turns,
    })

    # SDK built-in tools we expose so the agent can read/grep/write the
    # session workspace. These names are PascalCase string literals expected
    # by ClaudeAgentOptions (verified against claude_agent_sdk 0.1.81 types.py).
    # `cwd` does NOT sandbox file access, and `permission_mode=bypassPermissions`
    # means Read/Write/Grep/Glob would otherwise reach ANY path (incl. ../../.env
    # and other sites' credentials). Two guards confine them:
    #   1. Bash is NOT exposed (no discovery task needs a shell; this also closes
    #      the `printenv` path to the LLM-provider key in the subprocess env).
    #   2. A PreToolUse hook (fires even under bypassPermissions) confines every
    #      file-tool path to workspace_dir — see tool_guard.py.
    builtin_tools = ["Read", "Write", "Edit", "Grep", "Glob"]
    confinement_hooks = build_workspace_confinement_hooks(workspace_dir)

    # Windows CreateProcess caps the full command line at 32 KiB. Our system
    # prompt + flags + user prompt easily exceeds that (the prompt alone is
    # ~33 KB UTF-8 after the Fix-B evidence-anchor additions). Pass the prompt
    # via a file instead — the Claude Agent SDK exposes a `--system-prompt-
    # file` mode for exactly this case. The file lives inside `workspace_dir`
    # so it gets cleaned up with the session.
    system_prompt_path = workspace_dir / "system_prompt.md"
    # Phase 2: append the cross-domain regex index (a compact awareness list of
    # known URL-shape patterns) to the end of the prompt; per-entry detail comes
    # from lookup_skill. Empty string when the library is disabled/empty.
    # ...then the cross-run prose memory block (full set of notes, the chosen
    # read policy). Each renderer returns "" when its library is disabled/empty.
    from src.services.memory_library import render_memory_notes
    from src.services.skill_library import render_regex_index

    _skill_index = render_regex_index()
    _memory_notes = render_memory_notes()
    system_prompt_path.write_text(
        SYSTEM_PROMPT
        + (f"\n\n{_skill_index}" if _skill_index else "")
        + (f"\n\n{_memory_notes}" if _memory_notes else ""),
        encoding="utf-8",
    )

    options = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(system_prompt_path)},
        model=model,
        fallback_model=fallback_model,
        max_turns=max_turns,
        env=_deepseek_subprocess_env(),
        cwd=str(workspace_dir),
        mcp_servers={"zillusion-discovery": tools_server},
        tools=namespaced_tools + builtin_tools,
        allowed_tools=namespaced_tools + builtin_tools,
        permission_mode="bypassPermissions",
        hooks=confinement_hooks,
    )

    start = time.monotonic()
    final_text = ""
    cost_usd = 0.0
    duration_ms = 0.0
    num_turns = 0
    session_id: str | None = None

    turn_seq = 0  # increments on each AssistantMessage so child events can be re-grouped
    # task_description.md is the agent's MANDATORY first-step summary
    # (see system_prompt.py). When it appears for the first time, push
    # its content out as an SSE event so the user can verify the
    # agent's understanding before deeper work happens. We poll the
    # file's existence after each emitted event because the agent
    # writes via the Write builtin which we don't directly intercept.
    task_desc_path = workspace_dir / "task_description.md"
    # Content-dedup: emit the description the moment it first appears AND on every
    # later edit (the mandatory final-step reconcile, steer-driven rewrites) so the
    # user sees it immediately and watches it update live. Unchanged polls are
    # no-ops. (Previously it emitted ONCE — updates never reached the UI, and a
    # pre-existing file was never shown at all.)
    last_td_content: str | None = None

    async def _maybe_emit_task_description() -> AgentEvent | None:
        nonlocal last_td_content
        try:
            if not task_desc_path.exists():
                return None
            content = task_desc_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.debug("task_description read failed: %s", e)
            return None
        if not content.strip() or content == last_td_content:
            return None
        last_td_content = content
        log_event("task_description_committed", {
            "file_path": str(task_desc_path),
            "chars": len(content),
            "content": content,
        })
        return AgentEvent("task_description_committed", {
            "file_path": str(task_desc_path),
            "chars": len(content),
            "content": content,
        })

    # Build the prompt input. Default: a plain string (one-shot, unchanged
    # behavior). When a steer_queue is attached, use streaming-input mode (an
    # async iterable): the SDK drains it via a background task concurrently with
    # output (claude_agent_sdk client.py:218), so each yielded message reaches
    # the LIVE agent. CRITICAL: in this mode the session stays open until the
    # iterable EXHAUSTS — we MUST push a None sentinel when the agent is done
    # (on the terminal "done" event below) or the loop hangs forever.
    _input_closed = False
    if steer_queue is not None:
        # Clear a stale sentinel left by a prior reflect-loop iteration, but
        # preserve any real steer queued between iterations.
        _carry: list[Any] = []
        while not steer_queue.empty():
            try:
                _x = steer_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if _x is not None:
                _carry.append(_x)
        for _x in _carry:
            steer_queue.put_nowait(_x)

        async def _input_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_prompt},
                "parent_tool_use_id": None,
                "session_id": "",
            }
            while True:
                item = await steer_queue.get()
                if item is None:
                    return
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": str(item)},
                    "parent_tool_use_id": None,
                    "session_id": "",
                }

        prompt_arg: Any = _input_stream()
    else:
        prompt_arg = user_prompt

    try:
        async for msg in query(prompt=prompt_arg, options=options):
            if isinstance(msg, AssistantMessage):
                turn_seq += 1
                _log_agent_turn(msg, turn_seq)
            for ev in _message_to_events(msg, turn_seq):
                yield ev
                if ev.kind == "agent_started":
                    session_id = ev.data.get("session_id")
                    # NOTE: deliberately NOT logging session_id/cwd — per
                    # run_logger contract, those are SDK-debug noise.
                elif ev.kind == "tool_completed":
                    # Write to task_description.md surfaces as a generic
                    # tool_completed from the Write builtin; we can't tell
                    # from the SDK event which file was written. Poll the
                    # workspace after every tool_completed — the file
                    # check is one stat() call so cost is negligible.
                    td_ev = await _maybe_emit_task_description()
                    if td_ev is not None:
                        yield td_ev
                elif ev.kind == "agent_text":
                    # Use FULL text (set in _message_to_events as `text_full`)
                    # rather than the SSE-truncated `text`. Falling back to
                    # `text` preserves correctness for older callers that
                    # don't set `text_full`.
                    full_text = ev.data.get("text_full") or ev.data.get("text", "")
                    log_event("agent_text", {
                        "text": full_text,
                        "turn_seq": ev.data.get("turn_seq", turn_seq),
                    })
                elif ev.kind == "done":
                    cost_usd = float(ev.data.get("cost_usd") or 0.0)
                    duration_ms = float(ev.data.get("duration_ms") or 0.0)
                    num_turns = int(ev.data.get("num_turns") or 0)
                    final_text = str(ev.data.get("result") or "")
                    # Agent loop totals + the FULL final-text the agent
                    # emitted as its terminal message (this is where the
                    # JSON sources block lives — without `final_text` the
                    # run log records "agent done with 64 turns" but no
                    # trace of WHAT it actually concluded).
                    log_event("agent_done", {
                        "cost_usd": cost_usd,
                        "duration_ms": duration_ms,
                        "num_turns": num_turns,
                        "stop_reason": ev.data.get("stop_reason", ""),
                        "stop_reason_normalized": ev.data.get("stop_reason_normalized", "unknown"),
                        "is_error": bool(ev.data.get("is_error", False)),
                        "model_usage": ev.data.get("model_usage"),
                        "final_text": final_text,
                    })
                    # Streaming-input mode: the agent emitted its terminal
                    # result but the session stays open waiting for more stdin.
                    # Close the input (sentinel) so the query loop can end. Any
                    # steer already queued is delivered first (FIFO) before None.
                    if steer_queue is not None and not _input_closed:
                        _input_closed = True
                        try:
                            steer_queue.put_nowait(None)
                        except Exception:
                            pass
                elif ev.kind == "error":
                    log_event("error", {
                        "scope": "agent_sdk",
                        "message": ev.data.get("message", ""),
                    })
    except Exception as e:
        # Let the streaming-input background task exit instead of hanging on
        # steer_queue.get() until the transport is force-closed.
        if steer_queue is not None and not _input_closed:
            _input_closed = True
            try:
                steer_queue.put_nowait(None)
            except Exception:
                pass
        logger.exception("agentic.runner crashed: %s", e)
        log_event("error", {
            "scope": "agent_runner",
            "error_type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        })
        yield AgentEvent("error", {"message": f"{type(e).__name__}: {e}"})
        # Emit a result the node can inspect — but FLAG it as a crash via `error`
        # so it is NOT mistaken for a clean "0 sources found" success.
        elapsed_ms = (time.monotonic() - start) * 1000
        yield AgentRunResult(
            sources=[],
            cost_usd=cost_usd,
            duration_ms=elapsed_ms,
            num_turns=num_turns,
            session_id=session_id,
            raw_final_text=final_text,
            exploration_notes=f"crashed: {e}",
            error=f"{type(e).__name__}: {e}",
        )
        return

    # Defensive flush — in case the agent forgot
    try:
        await get_skill_library().flush()
    except Exception as e:
        logger.warning("agentic.runner defensive skill flush failed: %s", e)

    # ── Minimal backstop: enforce the MANDATORY FINAL STEP once ──
    # The system prompt asks the agent to reconcile task_description.md right
    # before emitting the final JSON, but that step has no tool gate, so a
    # non-compliant agent can skip it. Detect the dominant skip signal — the
    # agent committed sources/trees AFTER it last touched task_description.md, so
    # the description is stale relative to the committed results — and resume the
    # session ONCE to ask it to reconcile and re-emit. Best-effort: any failure
    # keeps the original final_text. Scope limits (acceptable for a minimal
    # backstop): it can't tell a mid-run edit from a genuine final reconcile, and
    # does nothing for final-JSON-only runs that committed nothing.
    if _FINAL_RECONCILE_NUDGE_ENABLED and session_id and final_text.strip():
        try:
            def _mt(p: Path) -> float:
                return p.stat().st_mtime if p.exists() else 0.0

            desc_mtime = _mt(task_desc_path)
            work_mtime = max(
                _mt(workspace_dir / "sources.jsonl"),
                _mt(workspace_dir / "portal_trees.jsonl"),
            )
            if desc_mtime > 0 and work_mtime > desc_mtime + 1e-6:
                log_event("task_description_reconcile_nudge", {
                    "reason": "committed_work_after_last_description_edit",
                    "desc_mtime": desc_mtime,
                    "work_mtime": work_mtime,
                })
                logger.info(
                    "agentic.runner: task_description.md stale vs committed work "
                    "(desc=%.1f < work=%.1f) — resuming once for reconcile",
                    desc_mtime, work_mtime,
                )
                # Cap the resume turn — it only needs Read + Edit + re-emit.
                nudge_opts = replace(
                    options, resume=session_id, max_turns=min(options.max_turns, 10)
                )
                async for msg in query(prompt=_RECONCILE_NUDGE_PROMPT, options=nudge_opts):
                    if isinstance(msg, AssistantMessage):
                        turn_seq += 1
                        _log_agent_turn(msg, turn_seq)
                    for ev in _message_to_events(msg, turn_seq):
                        if ev.kind == "done":
                            # Consume the resume turn's terminal event INTERNALLY
                            # — do not yield a second 'done' into the stream, or
                            # a consumer that treats 'done' as end-of-stream may
                            # close before the AgentRunResult.
                            new_final = str(ev.data.get("result") or "")
                            cost_usd += float(ev.data.get("cost_usd") or 0.0)
                            num_turns += int(ev.data.get("num_turns") or 0)
                            # Only adopt the reconcile turn's output if it
                            # re-emitted a parseable JSON block; a turn that only
                            # edited the file and said "done" must not wipe the
                            # real output.
                            replaced = bool(_extract_json_block(new_final))
                            if replaced:
                                final_text = new_final
                            log_event("task_description_reconcile_done", {
                                "replaced_final_text": replaced,
                                "stop_reason": ev.data.get("stop_reason", ""),
                            })
                            continue
                        yield ev
                        if ev.kind == "tool_completed":
                            # The reconcile turn edits task_description.md — surface
                            # that update live too (same poll as the main loop).
                            td_ev = await _maybe_emit_task_description()
                            if td_ev is not None:
                                yield td_ev
                        elif ev.kind == "agent_text":
                            log_event("agent_text", {
                                "text": ev.data.get("text_full") or ev.data.get("text", ""),
                                "turn_seq": ev.data.get("turn_seq", turn_seq),
                            })
        except Exception as e:
            logger.warning(
                "agentic.runner: reconcile-nudge resume failed: %s — "
                "keeping original output", e,
            )

    parsed = _extract_json_block(final_text) or {}
    final_json_src_dicts = parsed.get("sources") or []
    final_json_tree_dicts = parsed.get("portal_trees") or []
    exploration_notes = parsed.get("exploration_notes", "") or ""

    if not isinstance(final_json_src_dicts, list):
        logger.warning(
            "agentic.runner: parsed.sources is not a list (got %s)",
            type(final_json_src_dicts).__name__,
        )
        final_json_src_dicts = []
    if not isinstance(final_json_tree_dicts, list):
        logger.warning(
            "agentic.runner: parsed.portal_trees is not a list (got %s)",
            type(final_json_tree_dicts).__name__,
        )
        final_json_tree_dicts = []

    # ── Dual-track aggregation (emit-as-you-go + final-JSON fallback) ──
    # Preferred source of truth: incrementally committed JSONL written by
    # the new commit_source / commit_portal_tree tools. These persist
    # decisions at the moment of maximum context fidelity, side-stepping
    # the end-of-conversation narrative drift problem documented in
    # hotel-a3 / hotel-5c (Booking tree's 8 leaves → emitted children:[]).
    #
    # Fallback: the agent's final-message JSON block, parsed via
    # _extract_json_block above. We merge with priority to committed for
    # trees/sources that appear in both; final-JSON-only items are kept
    # as a supplement (covers cases where the agent forgot to commit, or
    # this query predates the emit-as-you-go rollout).
    session_dir = _get_committed_session_dir()
    committed_tree_dicts: list[dict] = []
    committed_src_dicts: list[dict] = []
    committed_stats: dict[str, int] = {}
    if session_dir is not None:
        committed_tree_dicts, committed_src_dicts, _tombstoned, committed_stats = (
            _load_committed_from_jsonl(session_dir)
        )

    tree_dicts, src_dicts, merge_stats = _merge_committed_and_final_json(
        committed_trees=committed_tree_dicts,
        committed_sources=committed_src_dicts,
        final_json_trees=final_json_tree_dicts,
        final_json_sources=final_json_src_dicts,
    )

    log_event("aggregation_sources", {
        "session_dir": str(session_dir) if session_dir else None,
        **committed_stats,
        **merge_stats,
    })

    # Build DataSource objects
    sources: list[DataSource] = []
    for src in src_dicts:
        if not isinstance(src, dict):
            continue
        sources.extend(_build_data_sources(src))

    # Build DataPageTree objects + collect every URL inside any tree for cross-dedup
    portal_trees, tree_urls, validation_stats = _build_portal_trees(tree_dicts)

    # Surface portal_tree evidence-validation outcomes to the run log so the
    # diagnostics_writeback node (and offline ReviewAgent) can count Fix B/E/D
    # violations per query and watch the rate trend over time. Warn-only mode
    # means trees with warnings are STILL included in `portal_trees` — the
    # counters tell us how often the new prompt rules are being honored.
    if validation_stats.get("total_warnings", 0) > 0:
        log_event("portal_tree_validation", {
            "n_trees": len(portal_trees),
            **validation_stats,
        })

    # Dedup: (a) flat-list internal dedup via canonicalize, (b) drop any
    # flat source whose canonical URL appears inside any tree (tree wins).
    deduped, dedup_stats = _apply_cross_structure_dedup(sources, tree_urls)

    # Surface dedup stats to the run log — the stdlib `logger.info` below
    # is human-readable only; without this event the JSONL run log can't
    # show how many sources the agent's raw JSON had before runner-side
    # deduplication, nor how many were dropped for cross-structure overlap.
    log_event("dedup_stats", {
        "raw_count": len(src_dicts),
        "after_dedup": len(deduped),
        "dropped_duplicate": dedup_stats.get("dropped_duplicate", 0),
        "dropped_in_tree": dedup_stats.get("dropped_in_tree", 0),
        "tree_urls_count": len(tree_urls),
        "n_portal_trees": len(portal_trees),
    })

    final_duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "agentic.runner complete: %d raw sources → %d after dedup "
        "(dropped %d intra, %d covered-by-tree), %d portal_trees, "
        "cost=$%.4f, turns=%d, %.1fs",
        len(src_dicts), len(deduped),
        dedup_stats["dropped_duplicate"], dedup_stats["dropped_in_tree"],
        len(portal_trees), cost_usd, num_turns, final_duration_ms / 1000,
    )

    yield AgentRunResult(
        sources=deduped,
        cost_usd=cost_usd,
        duration_ms=final_duration_ms,
        num_turns=num_turns,
        session_id=session_id,
        raw_final_text=final_text,
        exploration_notes=exploration_notes,
        portal_trees=portal_trees,
    )


async def run_discovery_agent(
    requirement: StructuredRequirement,
    **kwargs: Any,
) -> AgentRunResult:
    """Convenience non-streaming wrapper — drains events, returns final result."""
    result: AgentRunResult | None = None
    async for item in stream_discovery_agent(requirement, **kwargs):
        if isinstance(item, AgentRunResult):
            result = item
    if result is None:
        # Should not happen, but defensive
        result = AgentRunResult(
            sources=[], cost_usd=0.0, duration_ms=0.0,
            num_turns=0, session_id=None, raw_final_text="",
        )
    return result
