"""Stage 8: Finalize & Output — assemble the FinalReport.

Groups sources by type, generates usage guides, computes summary metrics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from src.config import settings
from src.models.data_source import DataSource, DataSourceType
from src.models.report import FinalReport
from src.models.state import AgentState
from src.services.llm import llm_service
from src.services.run_logger import log_event
from src.utils.request_context import get_request_cost

logger = logging.getLogger(__name__)

# Concurrency cap for per-source guide generation. Fast-tier semaphore
# upstream is 200; we cap at 30 here so finalize doesn't monopolize the
# pool while reflect/other late-stage callers may still be active.
_GUIDE_PER_SOURCE_MAX_CONCURRENT = 30

# Per-source markdown input budget (CHARACTERS). The discovery agent's
# fetched page markdown is fed VERBATIM (not truncated) into the guide
# LLM as ground truth. All-or-nothing: a page within this budget is sent
# whole; a page exceeding it is dropped entirely (never a half page) and
# the guide falls back to description + evidence, emitting a
# ``guide.markdown_skipped_oversized`` event so the run log shows which
# source was affected.
#
# 360_000 chars ≈ 120K tokens at a conservative 3 chars/token, leaving
# headroom under deepseek-v4-flash's 131K input limit for the prompt
# template + the 1024 output tokens. Raise toward ~390_000 to push closer
# to the provider ceiling; lower it to bound per-call cost.
_GUIDE_MD_CHAR_BUDGET = 360_000

# Mirrors tools._AGENT_SESSIONS_ROOT — the per-query workspace where
# fetch_page persists markdown (fetched/<hash>.md + fetched/_metadata.jsonl).
_AGENT_SESSIONS_ROOT = Path("agent-workspace/agent-sessions")

# Cover-points text per source type. The per-source prompt tells the LLM
# which specific aspects to address for an API / file / embedded source —
# tailored language ("how to access" vs "how to download" vs "how to
# extract") makes the LLM produce more useful guidance per type.
_GUIDE_COVER_POINTS = {
    "API": (
        "how to access",
        "- Where to sign up / get an API key (if any)\n"
        "- The documentation URL to start from\n"
        "- The auth method (API key / OAuth / none) and where the token goes\n"
        "- Rate limit / quota / pricing if known",
    ),
    "downloadable file": (
        "how to download",
        "- The direct download URL or page to navigate to\n"
        "- The file format (CSV / JSON / NetCDF / Parquet / ZIP / ...)\n"
        "- Column headers / fields if known\n"
        "- Any auth or registration needed to access the download",
    ),
    "embedded data": (
        "how to extract",
        "- What kind of page this is (single-page hub / search interface /\n"
        "  catalog / multi-record listing)\n"
        "- How to navigate to the actual data records\n"
        "- Recommended extraction approach (scrape HTML / parse JSON-LD /\n"
        "  hidden REST API / browser automation)\n"
        "- Any session, login, or rate-limit caveats",
    ),
}

PER_SOURCE_GUIDE_PROMPT = """Write a {verb} guide (3-5 sentences) for this single {type_name} source. The reader is a developer trying to start using this specific source.
{lang_line}
Cover:
{cover}

GROUND TRUTH — base the guide on the FETCHED PAGE CONTENT below. This is
the actual page the discovery agent read for this source; it is the most
reliable evidence available. Prefer concrete details that appear in it
(real endpoint paths, parameter names, auth instructions, download links,
file formats, signup steps) over your own prior knowledge.

CRITICAL — DO NOT HALLUCINATE:
- Cite only URLs, endpoint paths, parameter names, file formats, or extraction selectors that appear in the FETCHED PAGE CONTENT or the source's spec/facts below. If a detail is absent from both, say "consult the source's documentation" rather than inventing placeholders like "/api/v1/..." or "Authorization: Bearer YOUR_API_KEY".
- Do NOT state authentication requirements (API key / OAuth / signup) unless the page content, access_level, or spec explicitly says so. When unclear, say "check the source for any auth requirements".
- Do NOT fabricate rate limits, pricing, or coverage details that aren't supported by the content below.
- Keep it concrete and source-specific — do not give generic advice that would apply to any source.

Source:
{source_facts}
Access level: {access_level}
License: {license_text}

--- FETCHED PAGE CONTENT ---
{page_content}
--- END FETCHED PAGE CONTENT ---
"""


def _api_source_has_actionable_spec(s: DataSource) -> bool:
    """A source is "actionable" if its api_spec carries something a quickstart
    guide can quote beyond ``endpoint=<homepage>; method=GET``.

    Without this gate the LLM was producing boilerplate like
    "make a GET request to https://Polygon.io" for sources whose only
    discovered metadata was the brand homepage — no endpoint path, no auth
    info, no docs URL, nothing actionable. Surfacing that as "guidance" is
    actively misleading.
    """
    spec = s.api_spec
    if not spec:
        return False
    src_url_norm = (s.url or "").rstrip("/").lower()
    # documentation_url / signup_url / openapi_spec_url count as actionable
    # only when they carry info beyond the source homepage. process_by_type
    # defaults documentation_url=candidate.url for unenriched APIs, so a bare
    # equality check here would treat every web-found API as "actionable" and
    # produce boilerplate "consult their documentation" guides — see the
    # Polygon.io regression where docs=https://polygon.io = source URL.
    for url_field in (spec.documentation_url, spec.signup_url, spec.openapi_spec_url):
        if url_field and url_field.rstrip("/").lower() != src_url_norm:
            return True
    if spec.has_sdk:
        return True
    if spec.auth_type and spec.auth_type != "unknown":
        return True
    # An endpoint counts as actionable only if it differs from the source URL
    # itself (i.e. carries a real API path, not just the homepage).
    if spec.endpoint and spec.endpoint.rstrip("/").lower() != src_url_norm:
        return True
    return False


def _source_base_facts(s: DataSource) -> str:
    """Name + URL + FULL description + the agent's evidence note.

    Neither the description nor the evidence is truncated — these are the
    agent's own distillation of the source (description) and its notes
    from actually reading the page (metadata.evidence). When the fetched
    page markdown is dropped (oversized) or absent, this carries the
    high-signal fallback context the guide LLM grounds on, so a guide
    section never collapses to just a name + URL.
    """
    line = f"- {s.name} ({s.url}): {s.description or ''}"
    evidence = (s.metadata or {}).get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        line += (
            "\n  evidence (the discovery agent's notes from reading the "
            f"page): {evidence.strip()}"
        )
    return line


def _format_api_source_for_guide(s: DataSource) -> str:
    """Render an API source as a fact-list the LLM can quote without invention."""
    base = _source_base_facts(s)
    spec = s.api_spec
    if not spec:
        return base
    facts: list[str] = []
    # Emit endpoint as a fact only when it carries info beyond the homepage —
    # otherwise the LLM tends to anchor the guide on it as "the endpoint",
    # producing boilerplate like "make a GET request to https://Polygon.io".
    if spec.endpoint and spec.endpoint.rstrip("/") != (s.url or "").rstrip("/"):
        facts.append(f"endpoint={spec.endpoint}")
        if spec.method:
            facts.append(f"method={spec.method}")
    if spec.auth_type and spec.auth_type != "unknown":
        auth_desc = spec.auth_type
        if spec.auth_location:
            auth_desc += f" via {spec.auth_location}"
        if spec.auth_param_name:
            auth_desc += f" ({spec.auth_param_name})"
        facts.append(f"auth={auth_desc}")
    src_url_norm = (s.url or "").rstrip("/").lower()
    # Only surface signup/docs/openapi URLs when they point somewhere
    # different from the source homepage. A homepage URL repeated as "docs"
    # gives the LLM nothing real to quote and produces boilerplate.
    if spec.signup_url and spec.signup_url.rstrip("/").lower() != src_url_norm:
        facts.append(f"signup_url={spec.signup_url}")
    if spec.documentation_url and spec.documentation_url.rstrip("/").lower() != src_url_norm:
        facts.append(f"docs={spec.documentation_url}")
    if spec.openapi_spec_url and spec.openapi_spec_url.rstrip("/").lower() != src_url_norm:
        facts.append(f"openapi={spec.openapi_spec_url}")
    if spec.has_sdk:
        facts.append("has_sdk=true")
    return base + ("\n  spec: " + "; ".join(facts) if facts else "\n  spec: (none beyond URL)")


def _format_file_source_for_guide(s: DataSource) -> str:
    base = _source_base_facts(s)
    spec = s.file_spec
    if not spec:
        return base
    facts: list[str] = []
    if spec.download_url:
        facts.append(f"download_url={spec.download_url}")
    if spec.file_format:
        facts.append(f"format={spec.file_format}")
    if spec.compression:
        facts.append(f"compression={spec.compression}")
    if spec.column_headers:
        facts.append(f"columns={spec.column_headers[:8]}")
    return base + ("\n  spec: " + "; ".join(facts) if facts else "\n  spec: (none beyond URL)")


def _format_embedded_source_for_guide(s: DataSource) -> str:
    base = _source_base_facts(s)
    spec = s.embedded_spec
    if not spec:
        return base
    facts: list[str] = []
    if spec.extraction_method:
        facts.append(f"extraction_method={spec.extraction_method}")
    if spec.data_shape:
        facts.append(f"data_shape={spec.data_shape}")
    if spec.fields_present:
        facts.append(f"fields={spec.fields_present[:8]}")
    if spec.extraction_difficulty:
        facts.append(f"difficulty={spec.extraction_difficulty}")
    return base + ("\n  spec: " + "; ".join(facts) if facts else "\n  spec: (none beyond URL)")


_FORMATTERS = {
    "API": _format_api_source_for_guide,
    "downloadable file": _format_file_source_for_guide,
    "embedded data": _format_embedded_source_for_guide,
}


def _session_dir_for(query_id: str | None) -> Path | None:
    """Resolve the per-query session workspace dir from query_id.

    Mirrors tools._get_session_dir's sanitization so finalize reads from
    the same directory fetch_page wrote markdown into. Returns None when
    the dir doesn't exist (e.g. CLI/test paths with no agent run).
    """
    qid = (query_id or "").strip()
    if not qid:
        return None
    qid = qid.replace("/", "_").replace("\\", "_").replace("..", "_")[:64]
    sd = _AGENT_SESSIONS_ROOT / qid
    return sd if sd.exists() else None


def _build_markdown_index(session_dir: Path) -> dict[str, str]:
    """Map url → session-relative markdown path from fetched/_metadata.jsonl.

    fetch_page appends one record per fetch with {url, markdown_path}. We
    index both the raw url and its trailing-slash-stripped form so minor
    URL-shape differences between the committed source and the fetch
    record still resolve. Returns {} on any read failure (the per-source
    loader then falls back to the _url_hash path guess).
    """
    index: dict[str, str] = {}
    meta = session_dir / "fetched" / "_metadata.jsonl"
    if not meta.exists():
        return index
    try:
        for line in meta.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = rec.get("url")
            mp = rec.get("markdown_path")
            if u and mp:
                index[u] = mp
                index[u.rstrip("/")] = mp
    except OSError as e:
        logger.warning("guide markdown index build failed (%s): %s", meta, e)
    return index


def _strip_frontmatter(text: str) -> str:
    """Drop the leading ``<!-- url: ... -->`` comment block fetch_page writes."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (
        lines[i].strip().startswith("<!--") or not lines[i].strip()
    ):
        i += 1
    return "\n".join(lines[i:]).strip()


def _load_source_markdown(
    source: DataSource,
    session_dir: Path,
    md_index: dict[str, str],
) -> tuple[str | None, int]:
    """Return (full_markdown_or_None, char_count) for a source's fetched page.

    Resolution order:
      1. fetched/_metadata.jsonl index (by url, then trailing-slash-stripped)
      2. _url_hash(url) guess at fetched/<hash>.md

    The returned markdown is the FULL page body (frontmatter stripped),
    NOT truncated — the caller applies the all-or-nothing char budget.
    """
    url = source.url or ""
    rel = md_index.get(url) or md_index.get(url.rstrip("/"))
    if not rel:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        cand = session_dir / "fetched" / f"{h}.md"
        if cand.exists():
            rel = f"fetched/{h}.md"
    if not rel:
        return None, 0
    path = session_dir / rel
    if not path.exists():
        return None, 0
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, 0
    body = _strip_frontmatter(raw)
    return (body or None), len(body)


def _collect_tree_nodes(node: dict, out: list[tuple[str, str, str]]) -> None:
    """DFS-collect (url, page_type, markdown_path) for tree nodes that have
    saved markdown. Root first, then children in their sampled order — the
    order crawl_list_tree's two-level sampling produced (cluster by URL
    skeleton, then length-diverse per cluster), so children are already
    representative detail pages.
    """
    if not isinstance(node, dict):
        return
    mp = node.get("markdown_path")
    if mp:
        out.append((node.get("url") or "", node.get("page_type") or "", mp))
    for child in (node.get("children") or []):
        _collect_tree_nodes(child, out)


def _load_tree_markdown(
    source: DataSource,
    session_dir: Path,
) -> tuple[str | None, dict[str, int]]:
    """Assemble a portal tree's crawled markdown (root + sampled detail
    children) for the guide LLM, from metadata._original_tree.

    Budget policy (per the user's spec): if the whole tree's markdown fits
    in _GUIDE_MD_CHAR_BUDGET, feed ALL nodes. If it would exceed, fit as
    MANY whole nodes as possible — root first, then children in sample
    order — accumulating until the next node's full markdown wouldn't fit.
    A node too big for the remaining budget is skipped (never truncated)
    and the next node is still tried (box-packing). Per-node is
    all-or-nothing; the tree as a whole degrades gracefully.

    Returns (assembled_content_or_None, stats).
    """
    empty_stats = {"n_nodes": 0, "n_fed": 0, "n_skipped": 0, "chars": 0}
    original = (source.metadata or {}).get("_original_tree") or {}
    root = original.get("root") if isinstance(original, dict) else None
    if not isinstance(root, dict):
        return None, empty_stats

    nodes: list[tuple[str, str, str]] = []
    _collect_tree_nodes(root, nodes)
    if not nodes:
        return None, empty_stats

    assembled: list[str] = []
    used = 0
    n_fed = 0
    n_skipped = 0
    for url, page_type, mp in nodes:
        path = session_dir / mp
        if not path.exists():
            continue
        try:
            md = _strip_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if not md:
            continue
        # Per-node all-or-nothing within the shared tree budget. Box-packing:
        # a node that doesn't fit is skipped but later (smaller) nodes are
        # still tried, maximizing how much representative content gets in.
        if used + len(md) <= _GUIDE_MD_CHAR_BUDGET:
            header = f"## Page: {url} (page_type={page_type or 'unknown'})"
            assembled.append(f"{header}\n\n{md}")
            used += len(md)
            n_fed += 1
        else:
            n_skipped += 1

    stats = {
        "n_nodes": len(nodes), "n_fed": n_fed,
        "n_skipped": n_skipped, "chars": used,
    }
    if not assembled:
        return None, stats
    return "\n\n---\n\n".join(assembled), stats


async def _generate_per_source_section(
    source: DataSource,
    type_name: str,
    session_dir: Path | None = None,
    md_index: dict[str, str] | None = None,
    user_query: str = "",
) -> str:
    """Generate one source's usage guide section. Returns a markdown
    sub-section with the source name as H3 + URL line + LLM body.

    When the source's fetched page markdown is available (and within the
    char budget) it is fed VERBATIM to the LLM as ground truth, so the
    guide reflects what the page actually says rather than the model's
    prior knowledge. Oversized pages are dropped whole (logged) and the
    guide falls back to description + evidence.

    Used by :func:`_generate_guide` which fans out across all sources in
    a bucket in parallel. Fast-tier LLM. On LLM failure returns a stub
    section pointing the reader at the source URL — preserves ordering
    and never omits a source silently.
    """
    formatter = _FORMATTERS.get(
        type_name,
        lambda s: f"- {s.name} ({s.url}): {(s.description or '')[:120]}",
    )
    source_facts = formatter(source)
    verb, cover = _GUIDE_COVER_POINTS.get(
        type_name,
        ("how to use", "- How to access\n- How to extract data\n- Any auth requirements"),
    )

    # Load page content as ground truth (full, untruncated). Portal trees
    # feed root + sampled detail children from crawled/<csi>/; flat sources
    # feed their single fetched page.
    page_content = "(no page content was captured for this source)"
    is_tree = bool((source.metadata or {}).get("is_tree"))
    if session_dir is not None and is_tree:
        content, stats = _load_tree_markdown(source, session_dir)
        if content:
            page_content = content
        if stats["n_skipped"] > 0:
            # Tree exceeded budget — box-packed as many whole nodes as fit.
            log_event("guide.tree_markdown_partial", {
                "source_id": source.id,
                "url": (source.url or "")[:160],
                "n_nodes": stats["n_nodes"],
                "n_fed": stats["n_fed"],
                "n_skipped": stats["n_skipped"],
                "chars": stats["chars"],
                "budget": _GUIDE_MD_CHAR_BUDGET,
            })
            logger.info(
                "Stage 8 guide: tree %s fed %d/%d nodes (%d chars), "
                "skipped %d that didn't fit the budget",
                source.name[:40], stats["n_fed"], stats["n_nodes"],
                stats["chars"], stats["n_skipped"],
            )
    elif session_dir is not None and md_index is not None:
        body, n_chars = _load_source_markdown(source, session_dir, md_index)
        if body:
            if n_chars <= _GUIDE_MD_CHAR_BUDGET:
                page_content = body
            else:
                # All-or-nothing: an oversized page is dropped whole rather
                # than truncated. The guide degrades to description+evidence
                # for this one source; the event surfaces which source +
                # how big so the budget can be tuned later.
                log_event("guide.markdown_skipped_oversized", {
                    "source_id": source.id,
                    "url": (source.url or "")[:160],
                    "type_name": type_name,
                    "char_count": n_chars,
                    "budget": _GUIDE_MD_CHAR_BUDGET,
                })
                logger.info(
                    "Stage 8 guide: dropped oversized markdown for %s "
                    "(%d chars > %d budget) — using description+evidence",
                    source.name[:40], n_chars, _GUIDE_MD_CHAR_BUDGET,
                )

    # Write the guide in the user's language (inferred from their query). Keep
    # technical tokens canonical. Empty user_query → no directive (English default).
    lang_line = (
        f'\nLANGUAGE: write the guide prose in the SAME language as the user\'s '
        f'request — "{user_query.strip()[:200]}". Keep URLs, endpoint paths, '
        f'parameter names, file formats, and code identifiers in their canonical form.\n'
        if user_query.strip()
        else ""
    )
    prompt = PER_SOURCE_GUIDE_PROMPT.format(
        verb=verb,
        type_name=type_name,
        cover=cover,
        source_facts=source_facts,
        access_level=(source.access_level.value if source.access_level else "unknown"),
        license_text=(source.license or "unknown"),
        page_content=page_content,
        lang_line=lang_line,
    )

    try:
        guide_text, _ = await llm_service.complete(
            messages=[{"role": "user", "content": prompt}],
            profile="finalize",
            model_tier="fast",
            max_tokens=settings.llm.max_tokens_guide,
        )
        body = guide_text.strip() or "(no guidance generated)"
    except Exception as e:
        logger.warning(
            "Per-source guide failed for %s (%s): %s",
            source.name[:40], type_name, e,
        )
        body = (
            "Guide generation failed — visit the source URL above and "
            "consult its documentation for access details."
        )

    # Attach the guide body to the source itself so the UI can fold it into
    # each card (point 6). api_sources/file_sources/embedded_sources hold these
    # same objects, so the assignment flows straight into the FinalReport.
    source.usage_guide = body

    return f"### {source.name}\n\n**URL:** {source.url}\n\n{body}"


async def _generate_guide(
    sources: list[DataSource],
    type_name: str,
    session_dir: Path | None = None,
    md_index: dict[str, str] | None = None,
    user_query: str = "",
) -> str | None:
    """Generate a per-source usage guide for a source type bucket.

    Fans out parallel fast-tier LLM calls (one per source) bounded by
    ``_GUIDE_PER_SOURCE_MAX_CONCURRENT``. Concatenates the per-source
    sections in ranked order (input order is preserved by asyncio.gather)
    so the highest-rated sources lead the guide.

    Each source — including those with no api_spec / file_spec /
    embedded_spec — gets its own section. When ``session_dir`` /
    ``md_index`` are supplied, each source's fetched page markdown is fed
    to the LLM as ground truth. The prompt instructs the LLM to say
    "consult the source's documentation" rather than invent placeholders
    when neither the page content nor the spec carries a detail.
    """
    if not sources:
        return None

    sem = asyncio.Semaphore(_GUIDE_PER_SOURCE_MAX_CONCURRENT)

    async def _bounded(s: DataSource) -> str:
        async with sem:
            return await _generate_per_source_section(
                s, type_name, session_dir, md_index, user_query,
            )

    sections = await asyncio.gather(*[_bounded(s) for s in sources])
    return "\n\n---\n\n".join(sections)


_LOCALIZE_SUMMARY_PROMPT = """Rewrite the data-discovery summary below in the SAME language as the user's query, preserving every number and the exact meaning. If the query is in English, return the summary unchanged. Output ONLY the rewritten one-line summary, nothing else.

User query: {query}

Summary: {summary}"""


async def _localize_coverage_summary(summary: str, user_query: str) -> str:
    """Best-effort: render the (deterministic, English) coverage summary in the
    user's query language via one fast-tier LLM call. Falls back to the English
    summary on empty query or any failure. max_tokens is a hard upper bound;
    the output is a single line so it never approaches it."""
    q = (user_query or "").strip()
    if not q:
        return summary
    try:
        out, _ = await llm_service.complete(
            messages=[{"role": "user", "content": _LOCALIZE_SUMMARY_PROMPT.format(query=q[:300], summary=summary)}],
            profile="finalize",
            model_tier="fast",
            max_tokens=256,
        )
        return (out or "").strip() or summary
    except Exception as e:  # noqa: BLE001
        logger.warning("coverage_summary localization failed: %s", e)
        return summary


async def finalize_node(state: AgentState) -> dict:
    """Assemble the final discovery report."""
    start = time.monotonic()
    scored = state.get("scored_sources", [])
    requirement = state["requirement"]
    iteration = state.get("iteration", 1)

    # Group by type
    api_sources = [s for s in scored if s.source_type == DataSourceType.API]
    file_sources = [s for s in scored if s.source_type == DataSourceType.DOWNLOADABLE_FILE]
    embedded_sources = [s for s in scored if s.source_type == DataSourceType.EMBEDDED_DATA]

    # Emit per-source bucket decision so downstream debug can answer "where
    # did source X end up" without re-walking the FinalReport pydantic model.
    # Tree pseudo-sources land in embedded_sources because
    # tree_to_pseudo_source assigns source_type=EMBEDDED_DATA — surfacing
    # source_kind=tree here makes that mapping visible.
    for s in scored:
        bucket = s.source_type.value if s.source_type else "unknown"
        log_event("finalize.bucket", {
            "id": s.id,
            "name": s.name[:80],
            "url": s.url[:120],
            "bucket": bucket,
            "source_kind": "tree" if (s.metadata or {}).get("is_tree") else "flat",
            "discovery_method": getattr(s, "discovery_method", "") or "",
            "overall": round(s.scores.overall, 2) if s.scores else None,
            "relevance": round(s.scores.relevance, 2) if s.scores else None,
        })

    # Build a slim ranked list. The full DataSource (with its possibly-large
    # tier0_raw_extractions metadata blob) already lives in api/file/embedded
    # lists; serializing it twice was bloating responses dramatically.
    all_sources_ranked = [
        s.model_copy(update={"metadata": {}}) for s in scored
    ]
    logger.debug("Stage 8 source counts: API=%d, File=%d, Embedded=%d",
                  len(api_sources), len(file_sources), len(embedded_sources))

    # Resolve the per-query workspace + build the url→markdown index ONCE
    # so per-source guide generation can feed each source's fetched page
    # content to the LLM as ground truth. Done here (not per source) so the
    # _metadata.jsonl read happens a single time for the whole report.
    session_dir = _session_dir_for(state.get("query_id"))
    md_index: dict[str, str] = (
        _build_markdown_index(session_dir) if session_dir else {}
    )
    if session_dir is None:
        logger.info(
            "Stage 8 guide: no session dir for query_id=%r — guides will "
            "use description+evidence only (no fetched page content)",
            state.get("query_id"),
        )

    # Generate usage guides
    api_guide, file_guide, embedded_guide = None, None, None
    if scored:
        api_guide, file_guide, embedded_guide = await asyncio.gather(
            _generate_guide(api_sources, "API", session_dir, md_index, requirement.original_query),
            _generate_guide(file_sources, "downloadable file", session_dir, md_index, requirement.original_query),
            _generate_guide(embedded_sources, "embedded data", session_dir, md_index, requirement.original_query),
        )

    logger.debug("Stage 8 guides: api=%s, file=%s, embedded=%s",
                  api_guide[:100] if api_guide else None,
                  file_guide[:100] if file_guide else None,
                  embedded_guide[:100] if embedded_guide else None)

    # Coverage summary
    total = len(scored)
    type_dist = f"API: {len(api_sources)}, Files: {len(file_sources)}, Embedded: {len(embedded_sources)}"
    top_score = max((s.scores.overall for s in scored if s.scores), default=0)

    # Surface a warning when the user explicitly asked for csv/api/json/etc.
    # but the result set has no API or downloadable-file sources. Without this,
    # callers see a "Found N sources" line that obscures the format mismatch
    # — e.g. user requested CSV/API and we returned only an embedded portal
    # homepage, which cannot actually serve the data they asked for.
    structured_fmts = {"csv", "json", "xml", "parquet", "xlsx", "tsv", "api"}
    requested_structured = [
        f for f in (requirement.desired_formats or [])
        if str(f).lower().strip() in structured_fmts
    ]
    format_warning = ""
    if requested_structured and len(api_sources) == 0 and len(file_sources) == 0:
        format_warning = (
            f" Note: query requested {requested_structured} but no API or "
            f"downloadable-file sources surfaced — the result is empty or "
            f"contains only embedded/web pages, which may not satisfy the "
            f"format requirement without manual extraction."
        )

    coverage_summary = (
        f"Found {total} data sources ({type_dist}). "
        f"Top overall score: {top_score:.1f}/10. "
        f"Completed in {iteration} iteration(s).{format_warning}"
    )
    # Render it in the user's language (best-effort; English query → unchanged).
    coverage_summary = await _localize_coverage_summary(
        coverage_summary, requirement.original_query
    )

    # Calculate total candidates screened
    raw_count = len(state.get("raw_candidates", []))
    normalized_count = len(state.get("normalized_sources", []))

    logger.debug("Stage 8 screening funnel: raw=%d → normalized=%d → scored=%d",
                  raw_count, normalized_count, total)

    report = FinalReport(
        query=requirement.original_query,
        requirement=requirement,
        api_sources=api_sources,
        file_sources=file_sources,
        embedded_sources=embedded_sources,
        all_sources_ranked=all_sources_ranked,
        total_found=total,
        coverage_summary=coverage_summary,
        api_quickstart_guide=api_guide,
        file_download_guide=file_guide,
        embedded_extraction_guide=embedded_guide,
        iterations=iteration,
        total_candidates_screened=raw_count,
        processing_time_seconds=sum(state.get("stage_timings", {}).values()),
        # Prefer the request-scoped running total (covers LLM calls in
        # helper modules that discard the `usage` tuple element). Fall
        # back to whatever nodes explicitly accumulated on the state.
        estimated_cost_usd=max(
            get_request_cost(),
            float(state.get("cost_accumulated", 0) or 0),
        ),
    )

    # Persist the report to disk so file-based consumers (e.g. the
    # discovery→harness bridge, scripts/discovery_to_harness.py) can read it.
    # Best-effort: a write failure must not break the pipeline. session_dir is
    # None on CLI/test paths with no agent run — then the report is only
    # returned in state, as before.
    if session_dir:
        try:
            report_path = session_dir / "final_report.json"
            report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            logger.info("Stage 8 report persisted → %s", report_path)
        except Exception as exc:  # noqa: BLE001 — non-fatal
            logger.warning("Stage 8 report persist failed: %s", exc)

    duration = time.monotonic() - start
    logger.info(
        "Stage 8 complete: %d sources in report (%s), %.1fs",
        total, type_dist, duration,
    )

    return {
        "final_report": report,
        "stage_timings": {**state.get("stage_timings", {}), "finalize": duration},
    }
