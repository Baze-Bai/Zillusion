"""Agent SDK tool wrappers around existing backend capabilities.

Each tool is a thin adapter exposing one existing module to the LLM agent via
the MCP-in-process protocol. The tools are deliberately small and concrete so
the LLM can pick and choose; orchestration logic lives in the system prompt,
not in the tools.

Pattern: every tool returns a `dict[str, Any]` shaped as
``{"content": [{"type": "text", "text": json.dumps(payload)}]}`` because the
Claude Agent SDK's MCP transport expects this shape. We use JSON for the
``text`` payload so the LLM can parse structured results consistently.

All tools fail-soft: on exception they return ``{"error": "<msg>"}`` rather
than raising, so the agent can see the failure and react instead of the whole
session crashing.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from claude_agent_sdk import tool
from pydantic import BaseModel

from src.adapters.registry import AdapterRegistry, register_all_adapters
from src.config import settings
from src.models.data_source import DataSourceType
from src.services.llm import llm_service
from src.services.rate_limiter import rate_limited_call
from src.services.run_logger import get_run_logger, get_session_diagnostics, log_event
from src.services.memory_library import get_memory_library
from src.services.skill_library import Skill, SkillEntry, get_skill_library
from src.tools.scraping.firecrawl_client import (
    fetch_page_with_html,
    get_firecrawl,
)
from src.tools.net_guard import UnsafeURLError, assert_safe_url_async
from src.tools.search import brave, exa, searxng, tavily  # noqa: F401
from src.tools.search.base import BaseSearchTool
from src.tools.validation.head_prober import probe_url as _probe_url
from src.tools.validation.url_canonicalizer import canonicalize_url as _canonicalize

logger = logging.getLogger(__name__)


# ── Global I/O fan-out budget ─────────────────────────────────────────
# One process-wide cap on concurrently in-flight page fetches / registry
# queries / search calls across ALL tools. Individual tools bound their own
# internal parallelism (crawl per-level cap, classify LLM sem, firecrawl
# scrape sem), but nothing bounded the SUM — crawl + search_web +
# query_registry stacking their fan-outs is exactly the memory profile that
# OOM-killed a 31.6GB host (every in-flight fetch holds its response buffer).
# Value per operator decision 2026-06-10: 16 global / 4 crawl-level /
# 10 classify. Lazily created so the primitive binds to the running loop.
_FANOUT_LIMIT = 16
_fanout_sem: asyncio.Semaphore | None = None

# Per-level child concurrency inside crawl_list_tree's recursion. A hub page
# with 10+ link clusters × max_per_skeleton samples used to gather every
# subtree at once; nesting multiplied live coroutines exponentially by depth.
_CRAWL_CHILD_CONCURRENCY = 4


def _get_fanout_sem() -> asyncio.Semaphore:
    global _fanout_sem
    if _fanout_sem is None:
        _fanout_sem = asyncio.Semaphore(_FANOUT_LIMIT)
    return _fanout_sem


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


# ── Auto-pairing of tool_call ↔ tool_result ───────────────────────────
# Every @tool calls _log_tool_call(tool_name, args) at the top, but many
# error paths return _err(...) without first calling _log_tool_result.
# That leaves orphan tool_call events with no matching tool_result, which
# breaks lookups (e.g. commit_portal_tree → _lookup_tool_result_by_session)
# and makes debugging silent failures painful.
#
# Strategy: stash the active tool name in a ContextVar when _log_tool_call
# fires; clear it (and the "result emitted" flag) when _log_tool_result
# fires. _err() and _ok() — which are the only constructors of the return
# envelope — auto-emit a paired tool_result event when they fire while the
# tool is still "in-flight" (result not yet emitted). Tagged via
# ``signals=["err_unlogged_paired"]`` / ``["ok_unlogged_paired"]`` so the
# offline ReviewAgent can find and clean up these sites over time.
#
# ContextVar isolation is per-asyncio-task (PEP 567), so concurrent tool
# calls in asyncio.gather don't trample each other's state.
_active_tool_call: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_tool_call", default=None,
)
_result_emitted: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "result_emitted", default=False,
)


def _maybe_emit_paired_result(envelope_kind: str, payload: dict[str, Any]) -> None:
    """Auto-emit a tool_result when one is missing, just before the tool's
    envelope returns. ``envelope_kind`` ∈ {"err", "ok"}; ``payload`` is the
    error/result dict the envelope wraps (we surface a few fields into the
    paired summary for downstream search).
    """
    tool = _active_tool_call.get()
    if not tool or _result_emitted.get():
        return
    summary: dict[str, Any] = {"outcome": envelope_kind, "auto_paired": True}
    # Surface a few likely-useful fields without exposing the whole payload.
    for k in ("error", "url", "csi", "crawl_session_id", "session_id",
              "recommended_action", "missing", "kind"):
        if k in payload:
            summary[k] = payload[k]
    signals = [f"{envelope_kind}_unlogged_paired"]
    log_event("tool_result", {"tool": tool, "summary": summary, "signals": signals})
    _result_emitted.set(True)


def _ok(payload: Any) -> dict[str, Any]:
    """Wrap a JSON-serializable payload in the MCP text-content envelope."""
    if isinstance(payload, dict):
        _maybe_emit_paired_result("ok", payload)
    return {"content": [{"type": "text", "text": json.dumps(payload, default=_default_json, ensure_ascii=False)}]}


def _err(msg: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": msg}
    payload.update(extra)
    _maybe_emit_paired_result("err", payload)
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _log_tool_call(tool_name: str, args: dict[str, Any]) -> None:
    """Emit a structured ``tool_call`` event with the FULL input.

    No truncation, per "全部保存" requirement: list inputs (e.g.
    ``cluster_urls_by_skeleton(urls=[200 items])``) and long strings
    flow through unmodified. Log file size grows accordingly — acceptable
    given the explicit requirement that every record be preserved.

    Side effect: sets the contextvar so _err() / _ok() can auto-pair a
    tool_result event when the tool returns without explicitly logging one.
    """
    log_event("tool_call", {"tool": tool_name, "input": args})
    _active_tool_call.set(tool_name)
    _result_emitted.set(False)


def _log_tool_result(
    tool_name: str,
    summary: dict[str, Any],
    signals: list[str] | None = None,
) -> None:
    """Emit a structured ``tool_result`` event. ``summary`` should be a small dict
    of the key outcome fields, NOT the full payload.

    ``signals`` is an optional list of short tags flagging anomalous outcomes
    of this single call (``zero_results``, ``duplicate_fetch``, ``slow_call``,
    ``adapter_fallback``, …). Consumed by the diagnostics_writeback node at
    query end and by the external ReviewAgent offline. Free to add new tags
    — the consumer only counts what it sees.
    """
    payload: dict[str, Any] = {"tool": tool_name, "summary": summary}
    if signals:
        payload["signals"] = signals
    log_event("tool_result", payload)
    _result_emitted.set(True)


# ──────────────────────────────────────────────────────────────────────
# Emit-as-you-go session infrastructure
#
# The agent commits portal_trees and flat sources INCREMENTALLY via the
# commit_* tools, instead of accumulating them in memory and emitting one
# final JSON blob at end of run. This prevents the "context decay" failure
# mode where the agent re-narrates tree structure from memory at the very
# end of a long conversation, losing detail URLs in the process.
#
# Layout (per /discover request):
#   agent-workspace/agent-sessions/<query_id>/
#     portal_trees.jsonl      ← one DataPageTree dict per line
#     sources.jsonl           ← one DataSource dict per line, plus tombstone
#                                records {_tombstone: True, tombstoned_url, reason}
#
# The session dir is keyed off the RunLogger's query_id so it's stable
# across reflect-loop iterations (one decision log per query, not per-iter).
# ──────────────────────────────────────────────────────────────────────


_AGENT_SESSIONS_ROOT = Path("agent-workspace/agent-sessions")


def _get_session_dir() -> Path:
    """Return the session-scoped directory for emit-as-you-go JSONL files.

    Falls back to ``agent-workspace/agent-sessions/anon/`` if no RunLogger
    is bound (e.g. unit tests calling the tool handler directly). Always
    creates the directory if missing — caller can rely on its existence.
    """
    rl = get_run_logger()
    qid = (getattr(rl, "query_id", None) or "anon").strip() or "anon"
    # Sanitize: forbid path separators so a hostile query_id can't escape
    qid = qid.replace("/", "_").replace("\\", "_").replace("..", "_")[:64]
    sd = _AGENT_SESSIONS_ROOT / qid
    try:
        sd.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.debug("session dir mkdir failed for %s: %s", sd, e)
    return sd


def _task_description_path() -> Path:
    """Path to the per-session task description file the main LLM writes
    as a MANDATORY first step before any external-IO tool call.

    The file is plain Markdown; the main LLM uses the SDK Write tool to
    create it and may Edit/Read it any time during the run. Its content
    is also used by _auto_classify_tree as the task_hint for the helper
    LLM's relevance judgment (preferred over RunLogger.query_text)."""
    return _get_session_dir() / "task_description.md"


def _check_task_description() -> dict[str, Any] | None:
    """Return an error envelope dict when task_description.md is missing,
    else None. External-IO tools call this at handler entry to enforce
    the "write task description first" requirement.

    Built-in file tools (Read/Write/Edit/Grep/Glob/Bash), pure-computation
    tools (canonicalize_url / cluster_urls_by_skeleton / sample_cluster),
    skill operations (lookup_skill / propose_skill / flush_skills), and
    commit_* tools are exempt — only outward-facing network IO is gated.
    """
    if not _task_description_path().exists():
        return _err(
            "task_description.md is missing. As MANDATORY FIRST STEP, "
            "write a Markdown description of the user's data discovery "
            "goal to ./task_description.md using the Write tool. The "
            "description should cover: what data the user wants (records "
            "/ fields / format), the topic and domain, any constraints "
            "(geographic / temporal / license / quality), specific "
            "publishers the user named, and your understanding of "
            "'good enough' for this query. Example: "
            "Write('task_description.md', '## User goal\\n\\nThe user "
            "wants ...\\n\\n## Constraints\\n\\n...'). All external-IO "
            "tools (search_web / fetch_page / firecrawl_map / "
            "crawl_list_tree / probe_url / query_registry, and their "
            "batch variants) refuse to run until this file exists. You "
            "may Read and Edit it any time as your understanding deepens.",
            reminder="write_task_description_first",
        )
    return None


def _read_task_description() -> str:
    """Return task_description.md content as a single string, or empty
    when missing / unreadable."""
    try:
        return _task_description_path().read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _session_jsonl(filename: str) -> Path:
    """Return Path to a session-scoped JSONL file (portal_trees.jsonl /
    sources.jsonl). Filename is appended to _get_session_dir() — no
    subdirectories supported."""
    return _get_session_dir() / filename


def _append_to_jsonl(path: Path, record: dict[str, Any]) -> bool:
    """Append one JSON-serializable record as a single line. Returns True
    on success, False on any error (caller should surface to agent)."""
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=_default_json) + "\n")
        return True
    except Exception as e:
        logger.warning("jsonl append failed for %s: %s", path, e)
        return False


def _safe_canon_for_commit(url: str) -> str:
    """Best-effort URL canonicalization for emit-as-you-go dedup keys.

    Falls back to lowercase + strip slash when the proper canonicalizer
    can't parse (rare — usually malformed/relative URLs). The key is
    consumed only by the commit tools' dedup checks; downstream consumers
    use the original URL.
    """
    if not url:
        return ""
    try:
        return _canonicalize(url)
    except Exception:
        return url.strip().lower().rstrip("/")


def _iter_jsonl(path: Path):
    """Yield parsed JSON records from a JSONL file, skipping malformed
    lines silently. Returns nothing if the file doesn't exist."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception as e:
        logger.warning("jsonl read failed for %s: %s", path, e)
        return


def _walk_node_urls(node: dict[str, Any], acc: set[str]) -> None:
    """Recursively collect canonical URLs from a DataPageNode dict."""
    if not isinstance(node, dict):
        return
    u = node.get("url")
    if u:
        c = _safe_canon_for_commit(u)
        if c:
            acc.add(c)
    for child in (node.get("children") or []):
        _walk_node_urls(child, acc)


def _scan_session_trees_for_url(canonical: str) -> dict[str, Any] | None:
    """Scan portal_trees.jsonl for any tree whose ROOT or any descendant
    URL canonicalizes to ``canonical``. Returns ``{tree_index, csi,
    matched_url, matched_at_depth, matched_at_root}`` on first hit, or
    None.

    Note: this is O(N_trees × N_nodes_per_tree) per call. Acceptable for
    the expected ~5 trees per session; if this grows we'll cache.
    """
    if not canonical:
        return None
    path = _session_jsonl("portal_trees.jsonl")
    for i, tree in enumerate(_iter_jsonl(path)):
        if not isinstance(tree, dict):
            continue
        root = tree.get("root") or {}
        if not isinstance(root, dict):
            continue
        # Build URL set for this tree and check membership
        urls: set[str] = set()
        _walk_node_urls(root, urls)
        if canonical in urls:
            root_canon = _safe_canon_for_commit(root.get("url", ""))
            return {
                "tree_index": i,
                "csi": tree.get("crawl_session_id"),
                "matched_url": canonical,
                "matched_at_root": canonical == root_canon,
            }
    return None


def _scan_session_sources_for_url(canonical: str) -> dict[str, Any] | None:
    """Scan sources.jsonl for an already-committed source whose URL
    canonicalizes to ``canonical``. Tombstoned URLs are NOT considered
    committed (an agent can re-commit after removing). Returns
    ``{source_index, name, url}`` on first hit, or None."""
    if not canonical:
        return None
    path = _session_jsonl("sources.jsonl")
    tombstoned = _collect_tombstoned_urls()
    if canonical in tombstoned:
        return None
    idx = 0
    for rec in _iter_jsonl(path):
        if not isinstance(rec, dict):
            continue
        if rec.get("_tombstone"):
            continue
        if _safe_canon_for_commit(rec.get("url", "")) == canonical:
            return {
                "source_index": idx,
                "name": rec.get("name", ""),
                "url": rec.get("url", ""),
            }
        idx += 1
    return None


def _collect_tombstoned_urls() -> set[str]:
    """Return canonicalized URLs that have been tombstoned in sources.jsonl
    (either via remove_committed_source or auto-tombstoned by commit_
    portal_tree). Read-time helper used by both scan + collect."""
    out: set[str] = set()
    for rec in _iter_jsonl(_session_jsonl("sources.jsonl")):
        if not isinstance(rec, dict) or not rec.get("_tombstone"):
            continue
        c = _safe_canon_for_commit(rec.get("tombstoned_url", ""))
        if c:
            out.add(c)
    return out


def _lookup_tool_result_by_session(csi: str) -> dict[str, Any] | None:
    """Scan the active run log JSONL for the tool_result of a crawl_list_
    tree or firecrawl_map call with the given session_id. Returns the
    full ``data`` payload (including summary + signals) or None.

    Searches BOTH layouts:
      - Top-level ``summary.session_id`` — legacy single-URL shape
        (firecrawl_map / crawl_list_tree before the parallel refactor).
      - Nested ``summary.per_url_summary[].session_id`` — post-refactor
        shape where the aggregate tool_result carries an array of
        per-URL summaries, each with its own session_id. Without this
        fallback, commit_portal_tree can't anchor to any csi coming
        out of the parallel tools.

    When a match is found in the per_url_summary array, the returned
    ``data`` has its ``summary`` swapped to the matched per-URL entry
    plus tool name + signals copied from the outer envelope, so callers
    can treat both legacy and new shapes uniformly.
    """
    if not csi:
        return None
    rl = get_run_logger()
    if rl is None or not getattr(rl, "path", None) or not rl.path.exists():
        return None
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
                if data.get("tool") not in ("crawl_list_tree", "firecrawl_map"):
                    continue
                summary = data.get("summary") or {}
                # Legacy: top-level session_id
                if summary.get("session_id") == csi:
                    return data
                # New (parallel tools): nested per_url_summary[]
                # Match either the outer session_id (firecrawl_map's /v1/map
                # session, crawl_list_tree's own session) or the inner
                # fallback_session_id (firecrawl_map's Playwright fallback
                # crawl_list_tree session — only present when fallback ran).
                # Both are valid crawl_session_id values the agent can pass.
                for per in (summary.get("per_url_summary") or []):
                    if not isinstance(per, dict):
                        continue
                    matched_via = None
                    if per.get("session_id") == csi:
                        matched_via = "per_url_summary.session_id"
                    elif per.get("fallback_session_id") == csi:
                        matched_via = "per_url_summary.fallback_session_id"
                    if matched_via:
                        return {
                            "tool": data.get("tool"),
                            "summary": per,
                            "signals": data.get("signals"),
                            "_via": matched_via,
                        }
    except Exception as e:
        logger.warning("run log scan for session_id=%s failed: %s", csi, e)
    return None


def _lookup_url_in_crawl_tool_calls(canonical: str) -> dict[str, Any] | None:
    """Scan the run log for any crawl_list_tree or firecrawl_map call that
    operated on this canonical URL. Returns ``{tool, session_id}`` on first
    hit. Used to enforce the "don't emit a crawled URL as flat source"
    anti-pattern."""
    if not canonical:
        return None
    rl = get_run_logger()
    if rl is None or not getattr(rl, "path", None) or not rl.path.exists():
        return None
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
                tool_name = data.get("tool")
                if tool_name not in ("crawl_list_tree", "firecrawl_map"):
                    continue
                summary = data.get("summary") or {}
                # crawl_list_tree stores it as seed_url; firecrawl_map as url
                tool_url = summary.get("seed_url") or summary.get("url") or ""
                if _safe_canon_for_commit(tool_url) == canonical:
                    return {
                        "tool": tool_name,
                        "session_id": summary.get("session_id"),
                        "seed_url": tool_url,
                    }
    except Exception as e:
        logger.warning("run log scan for url=%s failed: %s", canonical, e)
    return None


def _mark_url_fetched(url: str) -> bool:
    """Record ``url`` as fetched in this session and return True if it was
    already there (i.e. this is a duplicate fetch).

    No-op outside a /discover context (SessionDiagnostics is None) — returns
    False so tools called from CLI/tests don't see spurious duplicates.
    """
    diag = get_session_diagnostics()
    if diag is None or not url:
        return False
    if url in diag.fetched_urls:
        return True
    diag.fetched_urls.add(url)
    return False


def _default_json(obj: Any) -> Any:
    """Fallback JSON serializer for Pydantic, dataclass, enum, set, etc."""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "value"):  # Enum
        return obj.value
    return str(obj)


def _registrable_etld1(host: str) -> str:
    """Same eTLD+1 helper used in expand_portals / content_classify."""
    if not host:
        return ""
    parts = host.lower().split(".")
    if len(parts) < 2:
        return host.lower()
    return ".".join(parts[-2:])


# ──────────────────────────────────────────────────────────────────────
# Discovery tools
# ──────────────────────────────────────────────────────────────────────


_SEARCH_TOOLS_CACHE: dict[str, BaseSearchTool] | None = None


def _get_search_tools() -> dict[str, BaseSearchTool]:
    """Lazily instantiate the search engines. Cached at module level.

    NOTE: class names are ``SearXNGSearch`` / ``BraveSearch`` / ``TavilySearch``
    / ``ExaSearch`` — NOT ``...SearchTool``. The previous version of this
    function had the wrong suffix and silently caught the AttributeError in
    ``logger.debug``, leaving the cache empty so ``search_web`` always
    returned ``"no search engines configured"``.
    """
    global _SEARCH_TOOLS_CACHE
    if _SEARCH_TOOLS_CACHE is None:
        out: dict[str, BaseSearchTool] = {}
        declined: list[str] = []
        init_failed: list[dict[str, str]] = []
        for mod, cls_name in (
            (searxng, "SearXNGSearch"),
            (brave, "BraveSearch"),
            (tavily, "TavilySearch"),
            (exa, "ExaSearch"),
        ):
            try:
                inst = getattr(mod, cls_name)()
                if inst.is_configured():
                    out[inst.name] = inst
                    logger.info("agentic.search engine registered: %s", inst.name)
                else:
                    declined.append(cls_name)
                    logger.info("agentic.search engine %s declined (not configured)", cls_name)
            except Exception as e:
                # Log at WARNING — silent failure here is what hid the
                # class-name typo bug for the entire Phase 6 E2E run.
                init_failed.append({"class": cls_name, "error": f"{type(e).__name__}: {e}"})
                logger.warning("agentic.search engine init failed for %s: %s", cls_name, e)
        _SEARCH_TOOLS_CACHE = out
        if not out:
            logger.warning(
                "agentic.search: NO engines configured — search_web tool will "
                "always return 'no search engines configured'. Set SEARCH_SEARXNG_URL "
                "or any of SEARCH_{BRAVE,TAVILY,EXA}_API_KEY in backend/.env."
            )
        # Run-log: one-time snapshot of which engines are actually usable.
        # Once-per-process (the cache check above gates this), so the cost
        # is negligible. Helps diagnose "why did search_web fall back to
        # engine X" without having to dig through stdlib log lines.
        log_event("engines_ready", {
            "usable": sorted(out.keys()),
            "declined": declined,
            "init_failed": init_failed,
        })
    return _SEARCH_TOOLS_CACHE


_SEARCH_WEB_MAX_QUERIES_PER_CALL = 20


async def _search_one_query(query: str, max_results: int) -> dict[str, Any]:
    """Run one search query through the engine fallback chain (searxng →
    brave → tavily → exa). Persists results to search/<hash>.jsonl and
    appends to search/_index.jsonl. Returns a per-query result dict that
    includes ``elapsed_ms`` so the timing analyzer can read real per-query
    network latency directly out of run-log summaries.
    """
    import time as _time
    tools = _get_search_tools()
    preferred_order = ["searxng", "brave", "tavily", "exa"]
    engines_tried: list[str] = []
    t0 = _time.monotonic()
    for name in preferred_order:
        engine = tools.get(name)
        if engine is None:
            continue
        engines_tried.append(name)
        try:
            candidates = await engine.search(query, max_results=max_results)
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            results = [
                {
                    "url": c.url,
                    "title": c.title,
                    "snippet": c.snippet,
                    "source_engine": c.source_engine,
                }
                for c in candidates[:max_results]
            ]
            logger.info(
                "agentic.search_web engine=%s query=%r → %d results (%dms)",
                name, query[:80], len(results), elapsed_ms,
            )
            # Externalize-all-external: persist results to workspace.
            session_dir = _get_session_dir()
            search_dir = session_dir / "search"
            search_dir.mkdir(parents=True, exist_ok=True)
            query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
            out_path = search_dir / f"{query_hash}.jsonl"
            file_path: str = ""
            try:
                with out_path.open("w", encoding="utf-8") as f:
                    for r in results:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                file_path = out_path.relative_to(session_dir).as_posix()
                idx = search_dir / "_index.jsonl"
                with idx.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "query": query, "engine": name,
                        "n_results": len(results),
                        "file_path": file_path,
                        "searched_at": _now_iso(),
                    }, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning("search_web persist failed for %r: %s", query[:60], e)
            return {
                "query": query,
                "engine": name,
                "n_results": len(results),
                "file_path": file_path or None,
                "preview": results[:5],
                "results": results,
                "elapsed_ms": elapsed_ms,
                "engines_tried": engines_tried,
            }
        except Exception as e:
            logger.warning(
                "agentic.search_web engine=%s query=%r failed: %s — trying next",
                name, query[:60], e,
            )
    elapsed_ms = int((_time.monotonic() - t0) * 1000)
    return {
        "query": query,
        "error": "all configured search engines failed",
        "engines_tried": engines_tried,
        "elapsed_ms": elapsed_ms,
        "n_results": 0,
    }


@tool(
    "search_web",
    "Search the web. Pass one or more queries — runs them in parallel "
    f"(up to {_SEARCH_WEB_MAX_QUERIES_PER_CALL} per call). Persists each "
    "query's results to search/<query_hash>.jsonl. Returns "
    "{n_queries, n_failed, n_results_total, results} where each entry in "
    "results is {engine, query, n_results, file_path, preview, elapsed_ms} "
    "on success or {query, error, engines_tried} on per-query failure. "
    "Backed by SearXNG/Brave/Tavily/Exa with automatic engine fallback "
    "per query.",
    {"queries": list, "max_results": int},
)
async def search_web(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("search_web", args)
    gate = _check_task_description()
    if gate is not None:
        return gate

    # Accept the legacy {"query": str} shape too — convert to a single-
    # item list. Keeps the door open for any older agent transcript or
    # caller that still passes the old arg name; no warning emitted to
    # avoid noise in the run log.
    raw_queries = args.get("queries")
    if not raw_queries and args.get("query"):
        raw_queries = [args.get("query")]
    if not isinstance(raw_queries, list) or not raw_queries:
        return _err("queries must be a non-empty list of strings")
    queries = [
        str(q).strip() for q in raw_queries if str(q).strip()
    ][:_SEARCH_WEB_MAX_QUERIES_PER_CALL]
    if not queries:
        return _err("queries contained no valid entries")
    max_results = int(args.get("max_results") or 10)

    tools = _get_search_tools()
    if not tools:
        _log_tool_result(
            "search_web",
            {"n_queries": len(queries), "n_results_total": 0},
            signals=["no_engines_configured"],
        )
        return _err("no search engines configured")

    # Parallel fan-out — each query runs through its own engine fallback
    # chain. asyncio.gather lets all queries fly in the same wall window;
    # per-engine rate limits / connection pools apply downstream. Each query
    # also holds a global fan-out slot so search + crawl + registry calls
    # share one process-wide I/O budget.
    async def _search_guarded(q: str) -> dict[str, Any]:
        async with _get_fanout_sem():
            return await _search_one_query(q, max_results)

    per_query = await asyncio.gather(*[
        _search_guarded(q) for q in queries
    ])

    n_queries = len(per_query)
    n_failed = sum(1 for r in per_query if r.get("error"))
    n_results_total = sum(int(r.get("n_results") or 0) for r in per_query)
    elapsed_ms_max = max(int(r.get("elapsed_ms") or 0) for r in per_query)
    elapsed_ms_sum = sum(int(r.get("elapsed_ms") or 0) for r in per_query)
    files_changed = [
        r["file_path"] for r in per_query
        if r.get("file_path") and not r.get("error")
    ]

    signals: list[str] = []
    if n_failed == n_queries:
        signals.append("all_engines_failed")
    elif n_failed:
        signals.append(f"partial_failures:{n_failed}")
    if n_results_total == 0:
        signals.append("zero_results")
    if any(len(r.get("engines_tried") or []) > 1 and not r.get("error") for r in per_query):
        signals.append("engine_fallback")

    # Run-log: full per-query results (preview + full hits) for audit.
    _log_tool_result("search_web", {
        "n_queries": n_queries,
        "n_failed": n_failed,
        "n_results_total": n_results_total,
        "elapsed_ms": elapsed_ms_max,        # wall time = slowest query
        "elapsed_ms_sum": elapsed_ms_sum,    # sequential-equivalent time
        "per_query": [
            {k: v for k, v in r.items() if k != "results"}  # strip full hits
            for r in per_query
        ],
        "results_per_query": [r.get("results") or [] for r in per_query],
    }, signals=signals or None)

    # Agent-visible payload — preview only, no raw full result list.
    agent_results = [
        {k: v for k, v in r.items() if k != "results"}
        for r in per_query
    ]
    return _ok({
        "n_queries": n_queries,
        "n_failed": n_failed,
        "n_results_total": n_results_total,
        "results": agent_results,
        "files_changed": files_changed,
    })


@tool(
    "query_registry",
    "Query a curated data-source registry by worker tag. Persists results "
    "to registry/<worker>/<query_hash>.jsonl. Returns {worker_tag, "
    "adapters_queried, n_results, file_path, preview, files_changed}. "
    "Registries: OpenAlex / Semantic Scholar / HuggingFace / Kaggle / CKAN. "
    "Worker tags: 'academic', 'datasets', 'gov', 'kb', 'geo'.",
    {"worker_tag": str, "keywords": list, "max_per_adapter": int},
)
async def query_registry(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("query_registry", args)
    gate = _check_task_description()
    if gate is not None:
        return gate
    worker_tag = (args.get("worker_tag") or "").strip()
    keywords = args.get("keywords") or []
    max_per = int(args.get("max_per_adapter") or 10)
    if not worker_tag:
        return _err("empty worker_tag")
    if not isinstance(keywords, list) or not keywords:
        return _err("keywords must be a non-empty list of strings")

    # Self-bootstrap: register_all_adapters() is normally called in FastAPI
    # lifespan startup, but the agent might run outside that context (cron
    # job, standalone test, etc.). Calling it when the registry is empty is
    # idempotent — already-imported modules no-op.
    if not AdapterRegistry.get_all():
        register_all_adapters()

    adapters = AdapterRegistry.get_by_worker(worker_tag)
    if not adapters:
        _log_tool_result(
            "query_registry",
            {"worker_tag": worker_tag, "n_adapters": 0, "n_results": 0},
            signals=["no_adapter_for_tag"],
        )
        return _err(
            f"no adapters registered for worker_tag={worker_tag!r}",
            available_tags=sorted({t for a in AdapterRegistry.get_all() for t in a.worker_tags}),
        )

    all_results: list[dict[str, Any]] = []
    adapter_failures = 0

    async def _query_one(adapter) -> list[dict[str, Any]]:
        # Enforce the adapter's own declared RateLimitConfig — the dataclass
        # existed on every adapter but nothing read it, so a parallel fan-out
        # could burst past a portal's documented RPS. The limiter is keyed
        # per adapter and seeded from its requests_per_second; a global
        # fan-out slot is held for the duration so registry queries, page
        # fetches and crawls share one process-wide I/O budget.
        async with _get_fanout_sem():
            candidates = await rate_limited_call(
                f"adapter:{adapter.name}",
                lambda: adapter.search(keywords=keywords, filters={}),
                rate_per_s=adapter.rate_limit.requests_per_second,
            )
        return [
            {
                "adapter": adapter.name,
                "url": c.url,
                "title": c.title,
                "snippet": c.snippet,
                "known_type": c.known_type.value if c.known_type else None,
                "metadata": c.metadata,
            }
            for c in candidates[:max_per]
        ]

    # Parallel fan-out (was serial: one slow registry stalled all the rest).
    per_adapter = await asyncio.gather(
        *(_query_one(a) for a in adapters), return_exceptions=True
    )
    for adapter, res in zip(adapters, per_adapter):
        if isinstance(res, BaseException):
            adapter_failures += 1
            logger.warning("agentic.query_registry adapter=%s failed: %s", adapter.name, res)
            continue
        all_results.extend(res)

    logger.info(
        "agentic.query_registry worker=%s keywords=%s → %d adapters, %d results",
        worker_tag, keywords[:3], len(adapters), len(all_results),
    )
    signals: list[str] = []
    if not all_results:
        signals.append("zero_results")
    if adapter_failures > 0:
        signals.append("adapter_failure")
    # Externalize-all-external: persist results to workspace.
    session_dir = _get_session_dir()
    reg_dir = session_dir / "registry" / worker_tag
    reg_dir.mkdir(parents=True, exist_ok=True)
    key = json.dumps({"worker": worker_tag, "kw": sorted(keywords)},
                     ensure_ascii=False, sort_keys=True)
    query_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    out_path = reg_dir / f"{query_hash}.jsonl"
    file_path: str = ""
    files_changed: list[str] = []
    try:
        with out_path.open("w", encoding="utf-8") as f:
            for r in all_results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        file_path = out_path.relative_to(session_dir).as_posix()
        files_changed.append(file_path)
        # Cross-registry index (append-only)
        idx = session_dir / "registry" / "_index.jsonl"
        with idx.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "worker_tag": worker_tag,
                "keywords": keywords,
                "n_results": len(all_results),
                "file_path": file_path,
                "queried_at": _now_iso(),
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("query_registry persist failed for %s/%s: %s",
                       worker_tag, keywords[:3], e)

    preview = all_results[:5]

    _log_tool_result("query_registry", {
        "worker_tag": worker_tag,
        "n_adapters": len(adapters),
        "n_adapter_failures": adapter_failures,
        "n_results": len(all_results),
        "file_path": file_path,
    }, signals=signals or None)
    return _ok({
        "worker_tag": worker_tag,
        "adapters_queried": [a.name for a in adapters],
        "n_results": len(all_results),
        "file_path": file_path or None,
        "preview": preview,
        "files_changed": files_changed,
    })


# ──────────────────────────────────────────────────────────────────────
# Page fetching + validation tools
# ──────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """UTC timestamp in ISO 8601 for jsonl audit records."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


_FETCH_PAGE_MAX_URLS_PER_CALL = 30


async def _fetch_one_page(url: str, include_html: bool) -> dict[str, Any]:
    """Fetch one URL, persist markdown (+ optional HTML) to the session
    workspace, append a row to fetched/_metadata.jsonl, and return a
    per-URL result dict including ``elapsed_ms``.

    Failures return ``{url, error, elapsed_ms}`` — the parallel parent
    decides how to aggregate them. The underlying fetch_page_with_html
    has its own Semaphore(8) on firecrawl scrape, so even when N parallel
    calls fire here only 8 actually hit the network at a time.
    """
    import time as _time

    # SSRF guard: the agent (or injected page content) must not drive a fetch
    # to cloud-metadata / loopback / RFC1918. Blocked URLs return a clean error
    # the agent sees, not a network round-trip.
    try:
        await assert_safe_url_async(url)
    except UnsafeURLError as e:
        log_event("tool_error", {"tool": "fetch_page", "url": url, "error": f"blocked: {e}"})
        return {"url": url, "error": f"blocked (SSRF guard): {e}", "elapsed_ms": 0, "duplicate": False}

    # Track duplicates against canonical URL so trailing-slash / fragment
    # variants don't slip through. Falls back to raw url on canonicalize err.
    try:
        canon = _canonicalize(url)
    except Exception:
        canon = url
    is_duplicate = _mark_url_fetched(canon)

    t0 = _time.monotonic()
    try:
        # want_html mirrors the agent's include_html choice: when it only
        # wants markdown there's no reason to ask firecrawl for (and buffer)
        # the rawHtml of the page. The global fan-out slot bounds whole-call
        # concurrency — the firecrawl sem only covers the firecrawl branch,
        # leaving the jina/httpx fallbacks unbounded.
        async with _get_fanout_sem():
            raw_html, markdown = await fetch_page_with_html(url, want_html=include_html)
    except Exception as e:
        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        log_event("tool_error", {"tool": "fetch_page", "url": url, "error": f"{type(e).__name__}: {e}"})
        return {
            "url": url,
            "error": f"fetch failed: {type(e).__name__}: {e}",
            "elapsed_ms": elapsed_ms,
            "duplicate": is_duplicate,
        }
    elapsed_ms = int((_time.monotonic() - t0) * 1000)

    # Persist content to the session workspace. The agent's cwd is the same
    # session dir, so the returned `file_path` is a relative path the agent
    # can pass straight to Read/Grep without translation.
    session_dir = _get_session_dir()
    fetched_dir = session_dir / "fetched"
    fetched_dir.mkdir(parents=True, exist_ok=True)
    url_hash = _url_hash(url)
    fetched_at = _now_iso()

    files_changed: list[str] = []
    md_rel: str = ""
    if markdown:
        md_path = fetched_dir / f"{url_hash}.md"
        try:
            md_path.write_text(
                f"<!-- url: {url} -->\n"
                f"<!-- markdown_chars: {len(markdown)} -->\n"
                f"<!-- fetched_at: {fetched_at} -->\n\n{markdown}",
                encoding="utf-8",
            )
            md_rel = md_path.relative_to(session_dir).as_posix()
            files_changed.append(md_rel)
        except Exception as e:
            logger.warning("fetch_page md write failed for %s: %s", url[:60], e)

    html_rel: str = ""
    if include_html and raw_html:
        html_path = fetched_dir / f"{url_hash}.html"
        try:
            html_path.write_text(raw_html, encoding="utf-8")
            html_rel = html_path.relative_to(session_dir).as_posix()
            files_changed.append(html_rel)
        except Exception as e:
            logger.warning("fetch_page html write failed for %s: %s", url[:60], e)

    # Index: one line per fetch for downstream auditing + cross-tool lookup.
    meta_path = fetched_dir / "_metadata.jsonl"
    try:
        meta_record = {
            "url": url,
            "fetched_at": fetched_at,
            "markdown_path": md_rel or None,
            "html_path": html_rel or None,
            "markdown_chars": len(markdown),
            "html_chars": len(raw_html or "") if include_html else None,
            "duplicate": is_duplicate,
        }
        with meta_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(meta_record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("fetch_page metadata write failed for %s: %s", url[:60], e)

    title = _infer_title(markdown[:1500], url) if markdown else ""
    links_count = len(_extract_md_links(markdown, url)) if markdown else 0

    logger.info(
        "agentic.fetch_page url=%s md=%d html=%d -> %s (%dms)",
        url[:80], len(markdown), len(raw_html or ""), md_rel or "(no_md)",
        elapsed_ms,
    )
    return {
        "url": url,
        "file_path": md_rel,                       # "" when fetch returned no content
        "html_path": html_rel or None,
        "markdown_chars_total": len(markdown),
        "html_chars_total": len(raw_html or "") if include_html else None,
        "title": title,
        "links_count": links_count,
        "files_changed": files_changed,
        "duplicate": is_duplicate,
        "elapsed_ms": elapsed_ms,
    }


@tool(
    "fetch_page",
    "Fetch one or more web pages — runs them in parallel (up to "
    f"{_FETCH_PAGE_MAX_URLS_PER_CALL} per call; underlying firecrawl pool "
    "bounds true concurrency to ~8). Saves each markdown to "
    "fetched/<hash>.md and (when include_html=true) raw HTML to "
    "fetched/<hash>.html. Returns {n_urls, n_failed, results} where each "
    "result is {url, file_path, html_path, markdown_chars_total, "
    "html_chars_total, title, links_count, files_changed, duplicate, "
    "elapsed_ms} on success or {url, error, elapsed_ms, duplicate} on "
    "per-URL failure. Markdown body is on disk, not in the response. "
    "Prefer ONE call with N urls over N calls with 1 url each.",
    {"urls": list, "include_html": bool},
)
async def fetch_page(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("fetch_page", args)
    gate = _check_task_description()
    if gate is not None:
        return gate

    # Accept the legacy {"url": str} arg shape too — convert to a single-
    # item list. Silently maps to the new parallel path with N=1.
    raw_urls = args.get("urls")
    if not raw_urls and args.get("url"):
        raw_urls = [args.get("url")]
    if not isinstance(raw_urls, list) or not raw_urls:
        return _err("urls must be a non-empty list of strings")
    urls = [
        str(u).strip() for u in raw_urls if str(u).strip()
    ][:_FETCH_PAGE_MAX_URLS_PER_CALL]
    if not urls:
        return _err("urls contained no valid entries")
    include_html = bool(args.get("include_html", False))

    # Parallel fan-out — each URL runs its own fetch. The underlying
    # firecrawl_client owns a Semaphore(8) so only ~8 are simultaneously
    # in-flight at the network level, even if we hand it 30 at once.
    per_url = await asyncio.gather(*[
        _fetch_one_page(u, include_html) for u in urls
    ])

    n_urls = len(per_url)
    n_failed = sum(1 for r in per_url if r.get("error") or not r.get("file_path"))
    n_duplicate = sum(1 for r in per_url if r.get("duplicate"))
    n_empty = sum(
        1 for r in per_url
        if not r.get("error") and int(r.get("markdown_chars_total") or 0) == 0
    )
    elapsed_ms_max = max(int(r.get("elapsed_ms") or 0) for r in per_url)
    elapsed_ms_sum = sum(int(r.get("elapsed_ms") or 0) for r in per_url)
    files_changed = [
        fc for r in per_url for fc in (r.get("files_changed") or [])
    ]

    signals: list[str] = []
    if n_failed == n_urls:
        signals.append("all_failed")
    elif n_failed:
        signals.append(f"partial_failures:{n_failed}")
    if n_empty:
        signals.append(f"empty_content:{n_empty}")
    if n_duplicate:
        signals.append(f"duplicate_fetch:{n_duplicate}")
    if elapsed_ms_max > settings.diagnostics.slow_call_ms:
        signals.append("slow_call")

    _log_tool_result("fetch_page", {
        "n_urls": n_urls,
        "n_failed": n_failed,
        "n_duplicate": n_duplicate,
        "n_empty": n_empty,
        "elapsed_ms": elapsed_ms_max,        # wall time = slowest URL
        "elapsed_ms_sum": elapsed_ms_sum,    # sequential-equivalent time
        "per_url": [
            {k: v for k, v in r.items() if k != "files_changed"}
            for r in per_url
        ],
    }, signals=signals or None)

    return _ok({
        "n_urls": n_urls,
        "n_failed": n_failed,
        "n_duplicate": n_duplicate,
        "results": per_url,
        "files_changed": files_changed,
    })


# Threshold below which `firecrawl_map` is considered "sparse" and the
# `crawl_list_tree` fallback kicks in. self-hosted Firecrawl has no
# fire-engine search backend and most sites have a thin/missing sitemap,
# so `firecrawl_map` returns 0-3 links most of the time. The fallback
# renders the page via Playwright and extracts links from the rendered
# DOM — far more reliable for self-hosted deployments.
_FIRECRAWL_MAP_SPARSE_THRESHOLD = 5


_FIRECRAWL_MAP_MAX_URLS_PER_CALL = 10


async def _firecrawl_map_one_url(url: str, limit: int) -> dict[str, Any]:
    """Map ONE portal URL via Firecrawl /v1/map. When the same-publisher
    yield is sparse (< _FIRECRAWL_MAP_SPARSE_THRESHOLD), auto-falls back
    to ``_crawl_list_tree_one_url`` for a Playwright-rendered DOM crawl.

    Returns the per-URL payload dict (same shape the public tool used to
    return inside its ``_ok({...})`` envelope). No tool_call / tool_result
    events emitted here — outer parallel wrapper handles aggregate logging.
    """
    session_id = f"fcmap-{abs(hash(url)) % 0xffffffff:08x}"

    try:
        await assert_safe_url_async(url)
    except UnsafeURLError as e:
        log_event("tool_error", {"tool": "firecrawl_map", "url": url, "error": f"blocked: {e}"})
        return {"url": url, "session_id": session_id, "error": f"blocked (SSRF guard): {e}"}

    fc = get_firecrawl()
    if not fc.is_configured:
        return {
            "url": url,
            "session_id": session_id,
            "error": "firecrawl not configured",
            "mode": fc.mode,
        }

    try:
        result = await fc.map(url, limit=limit, timeout=30)
    except Exception as e:
        log_event("tool_error", {"tool": "firecrawl_map", "url": url, "error": f"{type(e).__name__}: {e}"})
        result = {"success": False, "links": [], "error": str(e)}

    parent_etld1 = _registrable_etld1(urlparse(url).netloc)
    links = result.get("links") or []
    same_publisher: list[str] = []
    seen: set[str] = set()
    for link in links:
        if not isinstance(link, str):
            continue
        cleaned = link.split("#", 1)[0].rstrip("/")
        if not cleaned or cleaned in seen:
            continue
        target_etld1 = _registrable_etld1(urlparse(cleaned).netloc)
        if target_etld1 == parent_etld1:
            seen.add(cleaned)
            same_publisher.append(cleaned)

    logger.info("agentic.firecrawl_map url=%s → %d total / %d same-publisher", url[:80], len(links), len(same_publisher))

    # ── Sparse-result fallback: invoke _crawl_list_tree_one_url ───────
    fallback_used: str | None = None
    fallback_tree: dict | None = None
    fallback_session_id: str | None = None
    fallback_page_kind_counts: dict[str, int] = {}
    fallback_total_pages: int = 0
    if len(same_publisher) < _FIRECRAWL_MAP_SPARSE_THRESHOLD:
        logger.info(
            "agentic.firecrawl_map sparse (%d same-publisher links) — "
            "falling back to crawl_list_tree for %s",
            len(same_publisher), url[:80],
        )
        log_event("firecrawl_map_fallback", {
            "url": url,
            "primary_links": len(same_publisher),
            "threshold": _FIRECRAWL_MAP_SPARSE_THRESHOLD,
            "fallback_tool": "crawl_list_tree",
        })
        try:
            fallback_session_id = f"fcmap-fb-{abs(hash(url)) % 0xffffff:06x}"
            fb_payload = await _crawl_list_tree_one_url(
                url=url,
                max_depth=2,
                max_per_skeleton=2,
                max_total_pages=10,
                skip_pagination=True,
                session_id=fallback_session_id,
            )
            if fb_payload.get("error"):
                raise RuntimeError(fb_payload["error"])
            fallback_tree = fb_payload.get("root") or {}
            fallback_page_kind_counts = fb_payload.get("page_kind_counts") or {}
            fallback_total_pages = fb_payload.get("total_pages_visited") or 0

            tree_urls: list[str] = []
            def _walk(n: dict) -> None:
                u = (n.get("url") or "").split("#", 1)[0].rstrip("/")
                if u and u not in seen:
                    tree_etld1 = _registrable_etld1(urlparse(u).netloc)
                    if tree_etld1 == parent_etld1:
                        seen.add(u)
                        tree_urls.append(u)
                for c in n.get("children") or []:
                    _walk(c)
            _walk(fallback_tree)
            same_publisher.extend(tree_urls)
            fallback_used = "crawl_list_tree"
            logger.info(
                "agentic.firecrawl_map fallback recovered %d additional URLs (%d total)",
                len(tree_urls), len(same_publisher),
            )
        except Exception as e:
            logger.warning(
                "firecrawl_map fallback to crawl_list_tree failed: %s — returning sparse primary result",
                e,
            )
            log_event("tool_error", {
                "tool": "firecrawl_map",
                "scope": "fallback",
                "error": f"{type(e).__name__}: {e}",
            })

    fallback_template = (
        _to_data_page_node_template(fallback_tree)
        if fallback_used and fallback_tree else None
    )
    if fallback_template:
        if fallback_session_id:
            _save_template_for_session(fallback_session_id, fallback_template)
        _save_template_for_session(session_id, fallback_template)

    session_dir = _get_session_dir()
    mapped_dir = session_dir / "mapped" / session_id
    mapped_dir.mkdir(parents=True, exist_ok=True)
    files_changed: list[str] = []
    links_path: str = ""
    if same_publisher:
        lp = mapped_dir / "links.txt"
        try:
            lp.write_text("\n".join(same_publisher) + "\n", encoding="utf-8")
            links_path = lp.relative_to(session_dir).as_posix()
            files_changed.append(links_path)
        except Exception as e:
            logger.warning("firecrawl_map links write failed for %s: %s", url[:60], e)

    PREVIEW_THRESHOLD = 20
    if len(same_publisher) <= PREVIEW_THRESHOLD:
        same_publisher_preview = same_publisher
    else:
        same_publisher_preview = (
            same_publisher[:10] + [f"... ({len(same_publisher) - 20} URLs omitted)"]
            + same_publisher[-10:]
        )

    template_path: str | None = None
    if fallback_template:
        tp_csi = fallback_session_id or session_id
        template_path = f"templates/{tp_csi}.json"
        files_changed.append(template_path)
    fallback_markdown_dir = (
        f"crawled/{fallback_session_id}/" if fallback_session_id else None
    )

    return {
        "url": url,
        "session_id": session_id,
        "total_links": len(links),
        "same_publisher_links_preview": same_publisher_preview,
        "same_publisher_links_total": len(same_publisher),
        "links_path": links_path or None,
        "same_publisher_links": (
            same_publisher if len(same_publisher) <= PREVIEW_THRESHOLD else None
        ),
        "fallback_used": fallback_used,
        "fallback_tree_root": fallback_tree if fallback_used else None,
        "data_page_node_template": fallback_template,
        "template_path": template_path,
        "fallback_markdown_dir": fallback_markdown_dir,
        "files_changed": files_changed,
        "fallback_session_id": fallback_session_id if fallback_used else None,
        "evidence_for_summary": {
            "total_links": len(links),
            "n_same_publisher": len(same_publisher),
            "fallback_used": fallback_used,
            **(
                {
                    "fallback_page_kind_counts": fallback_page_kind_counts,
                    "fallback_total_pages_visited": fallback_total_pages,
                }
                if fallback_used else {}
            ),
        },
    }


@tool(
    "firecrawl_map",
    "Get the URL skeleton of one or more portal sites — runs them in "
    f"parallel (up to {_FIRECRAWL_MAP_MAX_URLS_PER_CALL} per call). Each "
    "URL tries Firecrawl /v1/map first (sitemap + site: search). When the "
    "result is sparse (< 5 same-publisher links — common on self-hosted "
    "Firecrawl without fire-engine), auto-falls back to a Playwright DOM "
    "crawl for that URL. /v1/map is unbounded so parallelism is real; the "
    "fallback path shares the firecrawl scrape Semaphore(8) with other "
    "scrape callers. Returns {n_urls, n_failed, results: [per-URL]} where "
    "each per-URL entry has {url, session_id, same_publisher_links_preview, "
    "same_publisher_links_total, fallback_used, fallback_tree_root, "
    "data_page_node_template, evidence_for_summary, ...}. When fallback "
    "ran, the per-URL fallback_tree_root carries helper_classification on "
    "each node (auto-classify uses the /discover query as task_hint).",
    {"urls": list, "limit": int},
)
async def firecrawl_map(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("firecrawl_map", args)
    gate = _check_task_description()
    if gate is not None:
        return gate

    # Accept legacy {"url": str} shape — convert to single-item list.
    raw_urls = args.get("urls")
    if not raw_urls and args.get("url"):
        raw_urls = [args.get("url")]
    if not isinstance(raw_urls, list) or not raw_urls:
        return _err("urls must be a non-empty list of strings")
    urls = [
        str(u).strip() for u in raw_urls if str(u).strip()
    ][:_FIRECRAWL_MAP_MAX_URLS_PER_CALL]
    if not urls:
        return _err("urls contained no valid entries")
    limit = int(args.get("limit") or 200)

    per_url = await asyncio.gather(*[
        _firecrawl_map_one_url(u, limit) for u in urls
    ])

    n_urls = len(per_url)
    n_failed = sum(1 for r in per_url if r.get("error"))
    n_with_fallback = sum(1 for r in per_url if r.get("fallback_used"))
    n_zero_links = sum(
        1 for r in per_url
        if not r.get("error") and (r.get("same_publisher_links_total") or 0) == 0
    )
    files_changed = [
        fc for r in per_url for fc in (r.get("files_changed") or [])
    ]

    signals: list[str] = []
    if n_failed == n_urls:
        signals.append("all_failed")
    elif n_failed:
        signals.append(f"partial_failures:{n_failed}")
    if n_zero_links:
        signals.append(f"zero_links:{n_zero_links}")
    if n_with_fallback:
        signals.append(f"used_fallback:{n_with_fallback}")

    _log_tool_result("firecrawl_map", {
        "n_urls": n_urls,
        "n_failed": n_failed,
        "n_with_fallback": n_with_fallback,
        "per_url_summary": [
            {
                "url": r.get("url"),
                "session_id": r.get("session_id"),
                "total_links": r.get("total_links"),
                "n_same_publisher": r.get("same_publisher_links_total"),
                "fallback_used": r.get("fallback_used"),
                "fallback_session_id": r.get("fallback_session_id"),
                "error": r.get("error"),
            }
            for r in per_url
        ],
    }, signals=signals or None)

    return _ok({
        "n_urls": n_urls,
        "n_failed": n_failed,
        "n_with_fallback": n_with_fallback,
        "results": per_url,
        "files_changed": files_changed,
    })


@tool(
    "probe_url",
    "Send a HEAD request (with GET fallback). Returns {url, is_alive, "
    "status_code, content_type, content_length, response_time_ms}.",
    {"url": str, "timeout": float},
)
async def probe_url(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("probe_url", args)
    gate = _check_task_description()
    if gate is not None:
        return gate
    url = (args.get("url") or "").strip()
    timeout = args.get("timeout")
    if not url:
        return _err("empty url")
    try:
        result = await _probe_url(url, timeout=float(timeout) if timeout else None)
    except Exception as e:
        log_event("tool_error", {"tool": "probe_url", "url": url, "error": f"{type(e).__name__}: {e}"})
        _log_tool_result("probe_url", {"url": url}, signals=["probe_error"])
        return _err(f"probe failed: {e}", url=url)
    signals: list[str] = []
    if not result.is_alive:
        signals.append("dead_url")
    if result.response_time_ms and result.response_time_ms > settings.diagnostics.slow_probe_ms:
        signals.append("slow_probe")
    _log_tool_result("probe_url", {
        "url": result.url, "is_alive": result.is_alive,
        "status_code": result.status_code, "content_type": result.content_type,
        "response_time_ms": result.response_time_ms,
    }, signals=signals or None)
    return _ok({
        "url": result.url,
        "is_alive": result.is_alive,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "content_length": result.content_length,
        "has_attachment": result.has_attachment,
        "response_time_ms": result.response_time_ms,
        "redirect_url": result.redirect_url,
    })


@tool(
    "canonicalize_url",
    "Normalize a URL: lowercase host, strip utm_* / fragment, http→https. "
    "Returns the canonical form.",
    {"url": str},
)
async def canonicalize_url(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("canonicalize_url", args)
    url = (args.get("url") or "").strip()
    if not url:
        return _err("empty url")
    try:
        canonical = _canonicalize(url)
    except Exception as e:
        return _err(f"canonicalize failed: {e}", url=url)
    _log_tool_result("canonicalize_url", {"input": url, "canonical": canonical})
    return _ok({"input": url, "canonical": canonical})


# ──────────────────────────────────────────────────────────────────────
# User communication
# ──────────────────────────────────────────────────────────────────────


@tool(
    "send_user_message",
    "Send a short message directly to the USER, shown as a chat bubble in the "
    "conversation (left side, like a chat). Use it to TALK to the person watching "
    "the run: explain what you're doing or why, flag something that needs their "
    "attention or a decision, report a notable finding, or answer guidance they "
    "steered in. Write in the user's own language. This is one-way (the user "
    "replies by steering) and does NOT pause the run. Keep it brief and human — "
    "it is NOT a log, not step-by-step narration, and not for dumping data (use "
    "the workspace / commit_* tools for those).",
    {"message": str},
)
async def send_user_message(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("send_user_message", args)
    message = (args.get("message") or "").strip()
    if not message:
        return _err("empty message")
    # Delivery to the UI happens in the runner: stream_discovery_agent detects
    # this tool call in the SDK message stream and emits a PERSISTED
    # `agent_message` event from the full text (the ToolUseBlock carries it
    # untruncated). This handler only validates + acknowledges to the agent.
    _log_tool_result("send_user_message", {"chars": len(message)})
    return _ok({"delivered": True, "chars": len(message)})


# ──────────────────────────────────────────────────────────────────────
# Skill library tools
# ──────────────────────────────────────────────────────────────────────


@tool(
    "lookup_skill",
    "Look up a learned classification for a URL. Matches (etld1, url_path) "
    "against known regex patterns and returns, per match, the FOCUSED entry "
    "(predicted types + page_type/data_type/site_type/fields/caveats/notes) "
    "plus a `skill_ref` you cite if you reuse/correct it. Treat the result as "
    "a PRIOR to verify against the page, not ground truth. types=[] means "
    "'not a data source / skip'.",
    {"etld1": str, "url_path": str},
)
async def lookup_skill(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("lookup_skill", args)
    etld1 = (args.get("etld1") or "").strip().lower()
    url_path = (args.get("url_path") or "").strip()
    if not etld1:
        return _err("empty etld1")

    lib = get_skill_library()
    try:
        if url_path:
            patterns = await lib.lookup(etld1, url_path)
        else:
            # No path → return all patterns for the domain (LLM may scan them).
            dsf = await lib.load(etld1)
            patterns = dsf.skills
    except Exception as e:
        return _err(f"skill lookup failed: {e}", etld1=etld1)

    def _entry_view(p: Skill) -> dict[str, Any] | None:
        e = p.focused_entry(url_path) if url_path else (p.entries[0] if p.entries else None)
        if e is None:
            return None
        return {
            "types": [t.value for t in e.types],
            "page_type": e.page_type, "data_type": e.data_type, "site_type": e.site_type,
            "fields": list(e.fields), "caveats": e.caveats, "notes": e.notes,
            "exemplar_url": e.url,
        }

    matches = [
        {"skill_ref": f"{etld1}/{p.pattern_id}", "pattern_id": p.pattern_id,
         "regex": p.regex, "entry": _entry_view(p)}
        for p in patterns
    ]
    signals = (["no_skill_match" if url_path else "no_skills_for_domain"]
               if not matches else None)
    _log_tool_result("lookup_skill", {
        "etld1": etld1, "url_path": url_path or None, "n_hits": len(matches),
    }, signals=signals)
    return _ok({"etld1": etld1, "url_path": url_path or None, "matches": matches})


@tool(
    "propose_skill",
    "Record a learned classification: file an ENTRY (this URL's types + "
    "page_type/data_type/site_type/fields/caveats/notes) under a `regex` that "
    "generalizes the URL shape. If the regex/pattern_id already exists the "
    "entry is appended to its bucket. Persisted on flush_skills(). Use "
    "types=[] for 'not a data source / skip this shape'.",
    {
        "etld1": str,
        "pattern_id": str,
        "regex": str,
        "url": str,
        "types": list,
        "page_type": str,
        "data_type": str,
        "site_type": str,
        "fields": list,
        "caveats": str,
        "notes": str,
    },
)
async def propose_skill(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("propose_skill", args)
    etld1 = (args.get("etld1") or "").strip().lower()
    pattern_id = (args.get("pattern_id") or "").strip()
    regex_str = (args.get("regex") or "").strip()

    if not etld1 or not pattern_id or not regex_str:
        return _err("etld1, pattern_id, and regex are all required")

    try:
        re.compile(regex_str)
    except re.error as e:
        return _err(f"invalid regex: {e}", regex=regex_str)

    name_map = {
        "api": DataSourceType.API,
        "file": DataSourceType.DOWNLOADABLE_FILE,
        "embedded": DataSourceType.EMBEDDED_DATA,
    }
    types: list[DataSourceType] = []
    for raw in (args.get("types") or []):
        dtype = name_map.get(str(raw).strip().lower())
        if dtype and dtype not in types:
            types.append(dtype)

    entry = SkillEntry(
        url=(args.get("url") or "").strip(),
        types=types,
        page_type=(args.get("page_type") or "").strip(),
        data_type=(args.get("data_type") or "").strip(),
        site_type=(args.get("site_type") or "").strip(),
        fields=[str(f).strip() for f in (args.get("fields") or []) if str(f).strip()],
        caveats=(args.get("caveats") or "").strip(),
        notes=(args.get("notes") or "").strip()[:1000],
    )
    skill = Skill(pattern_id=pattern_id[:60], regex=regex_str, entries=[entry])
    lib = get_skill_library()
    try:
        await lib.propose(etld1, skill)
    except Exception as e:
        return _err(f"propose failed: {e}", etld1=etld1)

    logger.info(
        "agentic.propose_skill etld1=%s pattern_id=%s types=%s",
        etld1, pattern_id, [t.value for t in types],
    )
    _log_tool_result("propose_skill", {
        "etld1": etld1, "pattern_id": pattern_id, "types": [t.value for t in types],
    })
    return _ok({"staged": True, "etld1": etld1, "pattern_id": pattern_id})


@tool(
    "flush_skills",
    "Flush all staged skill proposals to disk atomically. Returns the "
    "count of skills written. Idempotent across calls — only newly-staged "
    "proposals are written each time.",
    {},
)
async def flush_skills(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("flush_skills", args)
    lib = get_skill_library()
    try:
        n = await lib.flush()
    except Exception as e:
        log_event("tool_error", {"tool": "flush_skills", "error": f"{type(e).__name__}: {e}"})
        return _err(f"flush failed: {e}")
    logger.info("agentic.flush_skills wrote %d skill changes", n)
    _log_tool_result("flush_skills", {"written": n})
    return _ok({"written": n})


# ──────────────────────────────────────────────────────────────────────
# Self-correction surface: update_skill / delete_skill / consolidate_skills.
# (record_skill_use removed 2026-06-04 — no confidence/counters; a skill is
# trusted by default and fixed/deleted on error via the closed loop.)
#
#   propose_skill      = file an entry under a regex (append to its bucket)
#   update_skill       = "I verified the page — fix this pattern/entry"
#   delete_skill       = "this pattern is wrong; remove it"
#   consolidate_skills = LLM merge-duplicates pass over a domain's patterns
# ──────────────────────────────────────────────────────────────────────


@tool(
    "update_skill",
    "Correct an existing pattern after you VERIFIED the page. Pass pattern_id "
    "+ only the fields to change: `regex` replaces the pattern's regex; "
    "`types`/`fields`/`caveats`/`notes`/`page_type`/`data_type`/`site_type` "
    "patch the entry for `url` (or the first entry). To remove a wholly-wrong "
    "pattern use delete_skill.",
    {
        "etld1": str,
        "pattern_id": str,
        "regex": str,
        "url": str,
        "types": list,
        "fields": list,
        "caveats": str,
        "notes": str,
        "page_type": str,
        "data_type": str,
        "site_type": str,
    },
)
async def update_skill(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("update_skill", args)
    etld1 = (args.get("etld1") or "").strip().lower()
    pattern_id = (args.get("pattern_id") or "").strip()
    if not etld1 or not pattern_id:
        return _err("etld1 and pattern_id are required")

    # Parse types only if provided.
    types_arg = args.get("types")
    types_parsed: list[DataSourceType] | None
    if types_arg is None:
        types_parsed = None
    else:
        name_map = {
            "api": DataSourceType.API,
            "file": DataSourceType.DOWNLOADABLE_FILE,
            "embedded": DataSourceType.EMBEDDED_DATA,
        }
        types_parsed = []
        for raw in types_arg:
            dtype = name_map.get(str(raw).strip().lower())
            if dtype and dtype not in types_parsed:
                types_parsed.append(dtype)

    # Build a partial patch from the entry-level fields that were provided.
    entry_patch: dict[str, Any] = {}
    for k in ("fields", "caveats", "notes", "page_type", "data_type", "site_type"):
        v = args.get(k)
        if v is None:
            continue
        if k == "fields":
            v = [str(f).strip() for f in (v or []) if str(f).strip()]
        entry_patch[k] = v

    lib = get_skill_library()
    try:
        changed = await lib.update(
            etld1,
            pattern_id,
            regex=args.get("regex"),
            url=args.get("url"),
            types=types_parsed,
            entry_patch=entry_patch or None,
        )
    except ValueError as e:
        return _err(f"update rejected: {e}", etld1=etld1, pattern_id=pattern_id)
    except Exception as e:
        log_event("tool_error", {"tool": "update_skill", "error": f"{type(e).__name__}: {e}"})
        return _err(f"update failed: {e}")

    _log_tool_result(
        "update_skill",
        {"etld1": etld1, "pattern_id": pattern_id, "changed": changed},
        signals=None if changed else ["update_noop"],
    )
    return _ok({"etld1": etld1, "pattern_id": pattern_id, "changed": changed})


@tool(
    "delete_skill",
    "Hard-remove a whole pattern (its regex + all entries) from a domain. "
    "Permanent — use update_skill to fix a pattern that is only partly wrong.",
    {"etld1": str, "pattern_id": str, "reason": str},
)
async def delete_skill(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("delete_skill", args)
    etld1 = (args.get("etld1") or "").strip().lower()
    pattern_id = (args.get("pattern_id") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not etld1 or not pattern_id:
        return _err("etld1 and pattern_id are required")

    lib = get_skill_library()
    try:
        removed = await lib.delete(etld1, pattern_id, reason=reason)
    except Exception as e:
        log_event("tool_error", {"tool": "delete_skill", "error": f"{type(e).__name__}: {e}"})
        return _err(f"delete failed: {e}")

    _log_tool_result(
        "delete_skill",
        {"etld1": etld1, "pattern_id": pattern_id, "removed": removed},
        signals=None if removed else ["delete_noop_not_found"],
    )
    return _ok({"etld1": etld1, "pattern_id": pattern_id, "removed": removed})


@tool(
    "consolidate_skills",
    "Run an LLM merge-duplicates pass over one domain's patterns. Collapses "
    "patterns that index the SAME URL shape into one (unioning their entries; "
    "keeping/widening one regex). Returns {before, after, merged_groups, "
    "llm_failed}.",
    {"etld1": str},
)
async def consolidate_skills(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("consolidate_skills", args)
    etld1 = (args.get("etld1") or "").strip().lower()
    if not etld1:
        return _err("etld1 is required")

    lib = get_skill_library()
    try:
        result = await lib.consolidate(etld1)
    except Exception as e:
        log_event("tool_error", {"tool": "consolidate_skills", "error": f"{type(e).__name__}: {e}"})
        return _err(f"consolidate failed: {e}")

    signals: list[str] = []
    if result.get("llm_failed"):
        signals.append("consolidate_llm_failed")
    if result.get("merged_groups", 0) > 0:
        signals.append("skills_consolidated")
    _log_tool_result(
        "consolidate_skills",
        {"etld1": etld1, **result},
        signals=signals or None,
    )
    return _ok({"etld1": etld1, **result})


# ──────────────────────────────────────────────────────────────────────
# Memory library — cross-run PROSE notes (narrative / strategy / why).
# The prose counterpart to the structured skill library: things that don't
# fit a regex→type entry. Full set is injected at session start; the agent
# grows it in-run with memory_append. Boundary:
#   URL shape → type/fields           → propose_skill (structured)
#   narrative / strategy / why / caveat → memory_append (prose, here)
# ──────────────────────────────────────────────────────────────────────


@tool(
    "memory_append",
    "Record a durable CROSS-RUN lesson as prose: a publisher access caveat, a "
    "discovery strategy that paid off (or didn't) for a class of query, or a "
    "DO-NOT anti-pattern. `topic` is a short slug (e.g. "
    "'hotel-api-access-tiers'); a timestamped section is appended to "
    "memory/<topic>.md and shown to all future runs. Use for narrative / "
    "strategy / why — NOT for URL-shape→type (that is propose_skill). Write "
    "only durable insights that will help a different future run, not "
    "run-specific scratch.",
    {"topic": str, "body": str},
)
async def memory_append(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("memory_append", args)
    topic = (args.get("topic") or "").strip()
    body = (args.get("body") or "").strip()
    if not topic or not body:
        return _err("topic and body are both required")
    lib = get_memory_library()
    try:
        slug = await lib.append(topic, body)
    except Exception as e:
        return _err(f"memory append failed: {e}", topic=topic)
    logger.info("agentic.memory_append topic=%s bytes=%d", slug, len(body))
    _log_tool_result("memory_append", {"topic": slug, "bytes": len(body)})
    return _ok({"written": bool(slug), "topic": slug})


@tool(
    "memory_read",
    "Read one cross-run memory note in full by topic slug. The full set of "
    "notes is already injected at the top of this session, so you usually do "
    "NOT need this — use it only to re-read a specific note. Returns empty "
    "content if the topic does not exist.",
    {"topic": str},
)
async def memory_read(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("memory_read", args)
    topic = (args.get("topic") or "").strip()
    if not topic:
        return _err("empty topic")
    lib = get_memory_library()
    try:
        text = await lib.read(topic)
    except Exception as e:
        return _err(f"memory read failed: {e}", topic=topic)
    _log_tool_result("memory_read", {"topic": topic, "found": bool(text)})
    return _ok({"topic": topic, "content": text})


# ──────────────────────────────────────────────────────────────────────
# Tool change proposal — agent → offline ReviewAgent
# (added 2026-05-21 as 3 tools; collapsed 3 → 1 on 2026-05-22 to reduce
# the propose_* near-name collision pressure on the MCP surface.)
#
# OPTIONAL channel for the agent to voice tool-level feedback during a
# run. One tool with a ``kind`` enum:
#
#   kind="new"    — "I wished I had X for this run"
#   kind="modify" — "tool Y was confusing / wrong here"
#   kind="remove" — "tool Z was useless / actively misleading"
#
# Each call appends one JSONL line to
# ``<workspace_dir>/<YYYY-MM-DD>/<query_id>.jsonl`` and never blocks
# anything downstream — the ReviewAgent picks them up out-of-band.
# Agent is free to NEVER call this; it is not on any critical path.
# ──────────────────────────────────────────────────────────────────────


_VALID_PRIORITIES = ("low", "medium", "high")


def _tool_proposals_path() -> Path | None:
    """Date-sharded JSONL path for this run's tool change proposals.

    Returns None when ``settings.tool_proposals.enabled`` is False so
    handlers can early-return with {disabled: True}.
    """
    if not settings.tool_proposals.enabled:
        return None
    rl = get_run_logger()
    qid = (getattr(rl, "query_id", None) or "anon").strip() or "anon"
    qid = qid.replace("/", "_").replace("\\", "_").replace("..", "_")[:64]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    root = Path(settings.tool_proposals.workspace_dir)
    p = root / today / f"{qid}.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.debug("tool_proposals path mkdir failed for %s: %s", p, e)
    return p


def _append_tool_proposal(record: dict[str, Any]) -> dict[str, Any]:
    """Append one JSONL line; enforce the per-query cap.

    Returns ``{accepted: bool, reason?: str, path?: str, count_after?: int}``.
    Never raises — disk errors are logged and surfaced as accepted=False.
    """
    path = _tool_proposals_path()
    if path is None:
        return {"accepted": False, "reason": "disabled"}

    cap = settings.tool_proposals.max_per_query
    existing = 0
    if cap > 0 and path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                existing = sum(1 for _ in f)
        except Exception as e:
            logger.debug("tool_proposals count failed for %s: %s", path, e)
        if existing >= cap:
            return {
                "accepted": False,
                "reason": "per_query_cap_reached",
                "cap": cap,
                "count_so_far": existing,
            }

    record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("tool_proposals write failed (%s): %s", path, e)
        return {"accepted": False, "reason": f"write_failed: {type(e).__name__}"}

    log_event("tool_proposal_recorded", {
        "kind": record.get("kind"),
        "target": record.get("target") or record.get("tool_name") or record.get("proposed_name"),
        "priority": record.get("priority"),
    })
    return {
        "accepted": True,
        "path": str(path),
        "count_after": existing + 1,
    }


def _normalize_priority(value: Any) -> str:
    """Coerce free-form priority strings to one of the valid tiers.

    Default 'medium' when missing or unrecognized — keeps the LLM from
    erroring out over a typo and gives ReviewAgent a stable bucket.
    """
    v = (str(value) if value is not None else "").strip().lower()
    return v if v in _VALID_PRIORITIES else "medium"


_VALID_PROPOSAL_KINDS = ("new", "modify", "remove")


@tool(
    "propose_tool_change",
    "OPTIONAL feedback channel to the offline ReviewAgent. Use when you "
    "want to suggest a change to the MCP tool surface based on what you "
    "observed in THIS run. NO penalty for never calling — only file "
    "when you have a concrete moment to cite. The ReviewAgent reads "
    "these alongside diagnostics and may eventually open a code PR; "
    "nothing happens during this run.\n\n"
    "kind decides what you're proposing:\n"
    "  - 'new':    you wished for a capability that doesn't exist.\n"
    "              Set target = your proposed short slug (e.g. "
    "'fetch_pdf_text'); desired_behavior is required.\n"
    "  - 'modify': an existing tool has confusing semantics / wrong "
    "defaults / missing parameter.\n"
    "              Set target = the existing tool name; "
    "desired_behavior is required (what it should do instead).\n"
    "  - 'remove': a tool was consistently useless or actively "
    "misleading.\n"
    "              Set target = the existing tool name; "
    "desired_behavior may be left empty.\n\n"
    "rationale and evidence are ALWAYS required. Evidence must "
    "reference specific moments in THIS run — not theoretical reasoning.",
    {
        "kind": str,              # "new" | "modify" | "remove"
        "target": str,             # proposed slug (new) OR existing tool name (modify/remove)
        "summary": str,            # one-line gist
        "rationale": str,          # the "why" — change request / removal reason / motivation
        "desired_behavior": str,   # what the tool should do (required for new/modify, optional for remove)
        "evidence": str,           # specific events in THIS run that motivate it
        "priority": str,           # low | medium | high
    },
)
async def propose_tool_change(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("propose_tool_change", args)
    kind = (args.get("kind") or "").strip().lower()
    if kind not in _VALID_PROPOSAL_KINDS:
        return _err(
            f"kind must be one of {list(_VALID_PROPOSAL_KINDS)!r}; got {kind!r}",
            valid_kinds=list(_VALID_PROPOSAL_KINDS),
        )

    target = (args.get("target") or "").strip()
    summary = (args.get("summary") or "").strip()
    rationale = (args.get("rationale") or "").strip()
    evidence = (args.get("evidence") or "").strip()
    desired = (args.get("desired_behavior") or "").strip()

    # Required-field validation. desired_behavior is required for
    # new/modify but optional for remove (you're not asking for new
    # behavior, you're asking for deletion).
    missing: list[str] = []
    if not target:
        missing.append("target")
    if not summary:
        missing.append("summary")
    if not rationale:
        missing.append("rationale")
    if not evidence:
        missing.append("evidence")
    if kind in ("new", "modify") and not desired:
        missing.append("desired_behavior")
    if missing:
        return _err(
            f"missing required fields for kind={kind!r}: {missing}. "
            f"Evidence must reference specific events in THIS run.",
            kind=kind, missing=missing,
        )

    record: dict[str, Any] = {
        "kind": kind,
        "target": target,
        "summary": summary,
        "rationale": rationale,
        "evidence": evidence,
        "priority": _normalize_priority(args.get("priority")),
    }
    if desired:
        record["desired_behavior"] = desired

    result = _append_tool_proposal(record)
    _log_tool_result(
        "propose_tool_change",
        {"kind": kind, "target": target, **result},
        signals=None if result["accepted"] else ["tool_proposal_rejected"],
    )
    if result["accepted"]:
        return _ok({"kind": kind, "target": target, **result})
    return _err(
        f"proposal not accepted: {result.get('reason')}",
        kind=kind, target=target, **result,
    )


# ──────────────────────────────────────────────────────────────────────
# URL skeleton clustering (pure Python, no network)
#
# Used by cluster_urls_by_skeleton + sample_cluster. The algorithm groups
# URLs by replacing variable segments (numeric IDs, UUIDs, slugs-with-digits)
# with the literal `{id}`, plus an L2 frequency-based refinement that catches
# pure-alphabetic slugs the regex pass would miss (the HuggingFace
# /datasets/<owner>/<name> case).
# ──────────────────────────────────────────────────────────────────────

from collections import defaultdict   # noqa: E402  (placed here for locality)
from urllib.parse import parse_qs as _parse_qs  # noqa: E402

_RE_PURE_DIGITS = re.compile(r"^\d+$")
_RE_UUID_HEX = re.compile(r"^[0-9a-fA-F]{8,}$")
# Slug with at least one digit AND at least TWO letters AND length ≥ 4
# (e.g. abc123, item-2024-q3, laptop-mk2). Length + letter-count guards filter
# out version markers like "v1" / "v2" / "v10" that would otherwise be
# wrongly tagged as IDs (failure mode B from the cluster_urls_by_skeleton
# design discussion).
_RE_SLUG_WITH_ID = re.compile(
    r"^(?=(?:.*[a-zA-Z]){2,})(?=.*\d)[a-zA-Z0-9\-]{4,}$"
)

# File extensions to preserve so /list.html and /list.json stay in separate clusters.
_PATH_EXT_PRESERVED = (".html", ".php", ".htm", ".asp", ".aspx",
                       ".jsp", ".json", ".xml")


def _classify_segment_strict(seg: str) -> str:
    """Strict regex-based variable detection for one path segment.

    Returns either `{id}` (possibly with extension preserved) or the
    original segment unchanged.
    """
    if not seg:
        return seg
    base, ext = seg, ""
    if "." in seg:
        dot_idx = seg.rfind(".")
        potential_ext = seg[dot_idx:].lower()
        if potential_ext in _PATH_EXT_PRESERVED:
            base = seg[:dot_idx]
            ext = potential_ext
    if _RE_PURE_DIGITS.match(base):
        return "{id}" + ext
    if _RE_UUID_HEX.match(base):
        return "{id}" + ext
    if _RE_SLUG_WITH_ID.match(base):
        return "{id}" + ext
    return seg


def _strict_skeleton(url: str) -> str:
    """First-pass skeleton: strict regex applied to each segment + sorted query keys."""
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    path = parsed.path.rstrip("/") or "/"
    segs = path.split("/")
    out = [_classify_segment_strict(s) for s in segs]
    skeleton = "/".join(out) or "/"
    if parsed.query:
        qs = _parse_qs(parsed.query, keep_blank_values=False)
        if qs:
            skeleton += "?" + "&".join(f"{k}={{v}}" for k in sorted(qs.keys()))
    return skeleton


def _frequency_refine_skeleton(urls: list[str], k_threshold: int = 5) -> dict[str, str]:
    """L2 refinement (iterative): any (depth, normalized_prefix) position
    where the REAL segment varies across >= ``k_threshold`` distinct values
    is marked variable, even if the regex would have left it static.

    Iteration is required because marking depth=1 as variable changes the
    normalized prefix at depth=2, which may then ALSO cross the threshold.
    Example for HF datasets:
      iter 0: depth=1 prefix="/datasets" → {stanfordnlp, Yelp, ...} 5 distinct, mark variable
      iter 1: now skeleton at depth=2 prefix="/datasets/{id}" → 5 distinct names, mark variable
      iter 2: fixed point reached
    """
    if not urls:
        return {}

    # Parse each URL once
    parsed_data: list[tuple[str, list[str], str]] = []  # (url, segs, query)
    for url in urls:
        try:
            p = urlparse(url)
            segs = p.path.strip("/").split("/") if p.path.strip("/") else []
            parsed_data.append((url, segs, p.query or ""))
        except Exception:
            parsed_data.append((url, [], ""))

    # segment_skel[url][depth] = current classification of that segment
    # Start with strict regex per segment.
    segment_skel: dict[str, list[str]] = {}
    for url, segs, _ in parsed_data:
        segment_skel[url] = [_classify_segment_strict(s) for s in segs]

    # Iterate to fixed point (bounded to avoid pathological loops)
    for _ in range(10):
        changed = False

        # Count distinct REAL segment values at each (depth, normalized_prefix)
        by_pos: dict[tuple[int, str], set[str]] = defaultdict(set)
        for url, segs, _ in parsed_data:
            skel_segs = segment_skel[url]
            prefix = ""
            for depth, real_seg in enumerate(segs):
                if depth >= len(skel_segs):
                    break
                by_pos[(depth, prefix)].add(real_seg)
                tok = skel_segs[depth]
                prefix = f"{prefix}/{tok}" if prefix else f"/{tok}"

        # Promote any position with >= k_threshold distinct REAL values
        variable_positions = {
            pos for pos, seg_set in by_pos.items() if len(seg_set) >= k_threshold
        }

        # Apply: for each URL, if its (depth, current_prefix) is variable,
        # replace that segment with {id}.
        for url, segs, _ in parsed_data:
            skel_segs = segment_skel[url]
            prefix = ""
            for depth, real_seg in enumerate(segs):
                if depth >= len(skel_segs):
                    break
                if (depth, prefix) in variable_positions and real_seg:
                    cls = _classify_segment_strict(real_seg)
                    new_tok = cls if cls.startswith("{id}") else "{id}"
                    if skel_segs[depth] != new_tok:
                        skel_segs[depth] = new_tok
                        changed = True
                tok = skel_segs[depth]
                prefix = f"{prefix}/{tok}" if prefix else f"/{tok}"

        if not changed:
            break

    # Build final skeleton strings
    refined: dict[str, str] = {}
    for url, segs, query in parsed_data:
        skel_segs = segment_skel[url]
        if not skel_segs:
            skeleton = "/"
        else:
            skeleton = "/" + "/".join(skel_segs)
        if query:
            qs = _parse_qs(query, keep_blank_values=False)
            if qs:
                skeleton += "?" + "&".join(f"{k}={{v}}" for k in sorted(qs.keys()))
        refined[url] = skeleton

    return refined


def _sample_evenly_spaced(items: list[str], k: int) -> list[str]:
    """Pick K items spread evenly across the input list to avoid head-clustering."""
    if k <= 0:
        return []
    if len(items) <= k:
        return list(items)
    step = max(1, len(items) // k)
    sampled = items[::step][:k]
    # Fix off-by-one (integer division can drop the last bucket)
    if len(sampled) < k:
        sampled.append(items[-1])
    return sampled[:k]


def _sample_length_diverse(items: list[str], k: int) -> list[str]:
    """Sort by URL length, then pick K spread evenly across length distribution.

    Best for spotting outliers — the shortest and longest URLs in a cluster
    are most likely to be mis-classified (a category page hiding inside a
    detail-page cluster usually has notably shorter URL).
    """
    if k <= 0:
        return []
    if len(items) <= k:
        return sorted(items, key=len)
    sorted_by_len = sorted(items, key=len)
    indices = [i * (len(sorted_by_len) - 1) // (k - 1) for i in range(k)]
    return [sorted_by_len[i] for i in indices]


# ──────────────────────────────────────────────────────────────────────
# Cluster tools
# ──────────────────────────────────────────────────────────────────────


@tool(
    "cluster_urls_by_skeleton",
    "Cluster a list of URLs by path skeleton. Variable segments (numeric "
    "IDs, UUIDs, slugs-with-digits, AND segments that vary across >= 5 "
    "different values at the same path depth) are replaced with `{id}`; "
    "query values become `{v}`. Returns clusters sorted by count "
    "descending, each with count + example URLs.",
    {
        "urls": list,
        "max_clusters": int,
        "max_examples_per_cluster": int,
        "frequency_threshold": int,
    },
)
async def cluster_urls_by_skeleton(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("cluster_urls_by_skeleton", args)
    urls = args.get("urls") or []
    max_clusters = int(args.get("max_clusters") or 20)
    max_examples = int(args.get("max_examples_per_cluster") or 3)
    k_threshold = int(args.get("frequency_threshold") or 5)

    if not isinstance(urls, list) or not urls:
        return _err("urls must be a non-empty list of strings")

    # Filter to valid string URLs (defensive against agent passing weird types)
    valid_urls = [u for u in urls if isinstance(u, str) and u]
    if not valid_urls:
        return _err("no valid URL strings found in urls list")

    refined = _frequency_refine_skeleton(valid_urls, k_threshold=k_threshold)

    clusters: dict[str, list[str]] = defaultdict(list)
    for url in valid_urls:
        skel = refined.get(url, url)
        clusters[skel].append(url)

    sorted_clusters = sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:max_clusters]
    out_clusters = [
        {
            "skeleton": skeleton,
            "count": len(urls_in_cluster),
            "example_urls": _sample_evenly_spaced(urls_in_cluster, max_examples),
        }
        for skeleton, urls_in_cluster in sorted_clusters
    ]

    logger.info(
        "agentic.cluster_urls_by_skeleton: %d URLs → %d clusters (top-%d returned, k=%d)",
        len(valid_urls), len(clusters), len(out_clusters), k_threshold,
    )
    _log_tool_result("cluster_urls_by_skeleton", {
        "n_input": len(valid_urls),
        "n_clusters_total": len(clusters),
        "n_clusters_returned": len(out_clusters),
        "top_skeletons": [c["skeleton"] for c in out_clusters[:3]],
    })
    return _ok({
        "total_urls_input": len(valid_urls),
        "total_clusters_found": len(clusters),
        "clusters_returned": len(out_clusters),
        "frequency_threshold": k_threshold,
        "clusters": out_clusters,
    })


@tool(
    "sample_cluster",
    "Resample example URLs from a specific cluster (a skeleton produced "
    "by cluster_urls_by_skeleton). Pure local filtering + sampling. "
    "Strategies: 'evenly_spaced' (balanced positional sample), "
    "'length_diverse' (shortest + longest + spread), 'all' (every "
    "matching URL, ignoring max_examples).",
    {
        "urls": list,
        "skeleton": str,
        "max_examples": int,
        "strategy": str,
        "frequency_threshold": int,
    },
)
async def sample_cluster(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("sample_cluster", args)
    urls = args.get("urls") or []
    target_skeleton = (args.get("skeleton") or "").strip()
    max_examples = int(args.get("max_examples") or 10)
    strategy = (args.get("strategy") or "evenly_spaced").strip().lower()
    k_threshold = int(args.get("frequency_threshold") or 5)

    if not target_skeleton:
        return _err("`skeleton` is required (copy it verbatim from cluster_urls_by_skeleton output)")
    if not isinstance(urls, list) or not urls:
        return _err("`urls` must be the same non-empty list you passed to cluster_urls_by_skeleton")

    valid_urls = [u for u in urls if isinstance(u, str) and u]
    refined = _frequency_refine_skeleton(valid_urls, k_threshold=k_threshold)

    matching = [u for u in valid_urls if refined.get(u) == target_skeleton]
    if not matching:
        # Give the agent the list of skeletons that DID exist so it can self-correct
        available = sorted({s for s in refined.values()}, key=lambda s: -sum(1 for v in refined.values() if v == s))
        return _err(
            f"no URLs match skeleton {target_skeleton!r}",
            available_skeletons=available[:15],
            hint="Make sure you copied the skeleton string exactly from cluster_urls_by_skeleton output, "
                 "including {id}/{v} placeholders. The frequency_threshold here must match the one you "
                 "used in cluster_urls_by_skeleton (default 5).",
        )

    if strategy == "length_diverse":
        sampled = _sample_length_diverse(matching, max_examples)
    elif strategy == "all":
        sampled = matching
    else:
        if strategy != "evenly_spaced":
            logger.warning("agentic.sample_cluster: unknown strategy %r, using evenly_spaced", strategy)
        sampled = _sample_evenly_spaced(matching, max_examples)

    # Lightweight diagnostics — hints the agent on homogeneity at no extra cost
    lengths = [len(u) for u in matching]
    diagnostics = {
        "url_length_min": min(lengths),
        "url_length_max": max(lengths),
        "url_length_p50": sorted(lengths)[len(lengths) // 2],
        "url_length_spread_ratio": (max(lengths) / max(min(lengths), 1)) if lengths else 1.0,
    }

    logger.info(
        "agentic.sample_cluster: skeleton=%s matching=%d sampled=%d strategy=%s",
        target_skeleton, len(matching), len(sampled), strategy,
    )
    _log_tool_result("sample_cluster", {
        "skeleton": target_skeleton, "strategy": strategy,
        "n_matching": len(matching), "n_sampled": len(sampled),
    })
    return _ok({
        "skeleton": target_skeleton,
        "total_matching": len(matching),
        "strategy": strategy,
        "samples_returned": len(sampled),
        "samples": sampled,
        "diagnostics": diagnostics,
    })


# ──────────────────────────────────────────────────────────────────────
# Recursive list-page crawler (crawl_list_tree)
#
# Distinguished from `firecrawl_map`:
#   - firecrawl_map  → TRUE portals (catalog.data.gov, hf.co/datasets root).
#                      Uses sitemap+site: search; IGNORES query strings.
#   - crawl_list_tree → INTERIOR list pages (anything with ?search=, ?q=,
#                      ?filter=; mid-level catalog pages; search-result
#                      pages whose content depends on query string). Renders
#                      each page with Playwright so query filter takes effect,
#                      then extracts visible links and recurses on samples.
#
# Algorithm per node:
#   1. fetch_page_with_html  → markdown (reuses self-hosted firecrawl/jina/
#      httpx fallback chain).
#   2. Prefilter raw markdown links (5 layers, all pure-Python).
#   3. _is_list_page → decide list vs leaf via 3 signals (link homogeneity,
#      keyword scan, pagination markers).
#   4. If list: cluster_by_skeleton → sample 1-2 length-diverse per cluster
#      → recurse. Skip pagination skeletons by default.
#   5. Save full markdown to crawled/<session>/<hash>.md inside the agent
#      session workspace so the agent can Read(markdown_path) on demand.
# ──────────────────────────────────────────────────────────────────────

# hashlib moved to top-level imports — used by search_web / query_registry
# / fetch_page / _save_page_markdown for stable file-name hashing.
from urllib.parse import urljoin  # noqa: E402
from uuid import uuid4  # noqa: E402

# Markdown anchor regex: [text](href "optional title")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

# Static-asset extensions to drop at L1 (these aren't list items)
_STATIC_EXT = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".webp", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".tar",
    ".gz", ".rar", ".7z", ".mp4", ".mp3", ".webm", ".mov",
}

# Chrome subdomains (CDN / forum / help) — same eTLD+1 but off-purpose.
# These cleared L2 (eTLD+1) but should still drop because they're not list items.
_CHROME_SUBDOMAIN_RE = re.compile(
    r"^(cdn-?[\w-]*|cdn\d*|static\w*|assets?|asset\w*|img\w*|i\d+|media|"
    r"upload|file\w*|"
    r"discuss|forum|community|bbs|"
    r"help|support|kb)\.",
    re.IGNORECASE,
)

# L4 nav blacklist — common chrome paths. Anchored at start so `/help`
# matches but `/dataset/help-data` does not.
_NAV_PATH_RE = re.compile(
    r"^/(login|signin|sign-in|signup|sign-up|register|account|settings|"
    r"profile|notifications|help|faq|support|contact|privacy|terms|legal|"
    r"about|pricing|blog|news|feed|rss|sitemap|robots\.txt|"
    r"cart|checkout|favorites?|favourites?|wishlist)(/|$)",
    re.IGNORECASE,
)

# Pagination markers in URL paths — `?page=N`, `/page/N`, `?offset=N`, etc.
# Used both to flag pagination links (so we don't recurse on them) and to
# detect pagination clusters at the skeleton level.
_PAGINATION_PATH_RE = re.compile(
    r"(/page/\d+|/p\d+/|[?&](page|p|offset|start|skip)=\d+)",
    re.IGNORECASE,
)

# List-page keyword scan in rendered markdown — copy patterns that imply
# the page is an enumeration UI.
_LIST_KEYWORD_RE = re.compile(
    r"(showing\s+\d+|matching\s+\d+|\d+\s+results?|\d+\s+items?|"
    r"filter\s+by|sort\s+by|next\s+page|previous\s+page|"
    r"加载更多|筛选|排序|下一页|上一页|共\s*\d+\s*[个条家])",
    re.IGNORECASE,
)

# SPA-friendly list signals — patterns from rendered markdown TEXT (not
# from extracted links). These fire when a page semantically claims to
# enumerate many items even though its hotel/product cards are JS-rendered
# and never appear as <a> tags in the static markdown.
#
# Examples this catches (real cases from Ctrip / Booking / Agoda):
#   "约 11,264 家酒店"
#   "Showing 30 of 11,264 hotels"
#   "found 200 properties"
#   "共 11,264 条记录"
#   "Sort by price | rating | popularity"
#   "按价格 排序"
_SPA_LIST_TEXT_RE = re.compile(
    # Big number + countable noun (en + zh)
    r"\d[\d,]{2,}\s*(?:hotels?|listings?|properties|"
    r"results?|items?|products?|datasets?|"
    r"家|条|个|项|套|份|处|间)|"
    # "showing X of Y" / "found N" enumeration UI
    r"showing\s+\d+\s+(?:of\s+)?\d*|"
    r"found\s+\d+\s+(?:hotels?|results?|items?|properties)|"
    # Chinese count phrases
    r"共\s*\d[\d,]{0,7}\s*[家条个项套份处间]|"
    r"约\s*\d[\d,]{1,7}\s*家|"
    # Sort/filter UI ribbon (controls that gate a list, not a leaf)
    r"sort\s+by\s+(?:price|rating|popularity|relevance|distance)|"
    r"按\s*(?:价格|评分|距离|热门|人气)\s*(?:排序|筛选)",
    re.IGNORECASE,
)

# Where saved crawl markdown lives — session-scoped under the agent's
# workspace so the agent can Read/Grep nodes by relative path.
def _crawl_cache_dir(session_id: str) -> Path:
    """Return ``<session_dir>/crawled/<session_id>/`` for crawl markdown files.

    All page content fetched by ``crawl_list_tree`` (or the internal call
    made by ``firecrawl_map`` fallback) lands here so that:
    1. The agent can resolve ``markdown_path`` to a relative posix path it
       can pass straight to ``Read``/``Grep``.
    2. Cross-iteration audit has everything in one place — the JSONL +
       templates + fetched/ + crawled/ all live under the same query_id.
    """
    return _get_session_dir() / "crawled" / session_id


# ──────────────────────────────────────────────────────────────────────
# DataPageNode template generation (Fix A)
#
# Bridges the gap between crawl_list_tree's internal node format
# (page_kind, markdown_excerpt, list_signals, ...) and the DataPageNode
# shape the agent must emit (page_type, title, record_count, fields_
# available, ...). Returned in the tool envelope as
# `data_page_node_template` so the agent copies it verbatim instead of
# re-narrating the tree. This prevents the structural data loss seen in
# the hotel-a3 run, where Booking's 6 real leaves became `children: []`.
# ──────────────────────────────────────────────────────────────────────


# Ordered most-specific → most-general. First match wins per sample.
_RECORD_COUNT_PATTERNS = (
    # Explicit "Showing N of M results"
    re.compile(r"showing\s+\d+\s+of\s+([\d,]{1,8})", re.IGNORECASE),
    re.compile(r"found\s+([\d,]{1,8})\s+(?:hotels?|results?|properties|listings?|items?)", re.IGNORECASE),
    re.compile(r"([\d,]{1,8})\s+(?:hotels?|properties|listings?|results?|items?)\s+(?:found|available|in\s+\w+)", re.IGNORECASE),
    # Chinese count phrases with explicit "共/约"
    re.compile(r"共\s*([\d,]{1,8})\s*[家条个项套份处间]"),
    re.compile(r"约\s*([\d,]{1,8})\s*[家条个项套份]"),
    # Chinese plain "N家/条" — slightly noisier (might match phone numbers etc),
    # so anchor on uncommon suffix glyphs
    re.compile(r"([\d,]{2,8})\s*家"),
    re.compile(r"([\d,]{2,8})\s*条(?:评论|点评|住宿|房源)?"),
    # Generic "N hotels / N properties" without modifiers
    re.compile(r"([\d,]{2,8})\s+(?:hotels?|properties|listings?)\b", re.IGNORECASE),
)


def _infer_record_count(
    spa_samples: list[str], markdown_excerpt: str,
) -> int | None:
    """Best-effort extract of "how many records does this list page expose".

    Priority order:
      1. spa_list_text_samples from _is_list_page (already filtered to
         enumeration phrases like "11269家" / "Showing 30 of 1,775")
      2. First 800 chars of markdown_excerpt (catches headings/banners
         that didn't make it into spa_samples)

    Returns the LARGEST integer found across all matches — list pages
    typically display "Showing X of Y", and Y is the actual count.
    Returns None if nothing parsable.
    """
    candidates: list[int] = []
    for sample in (spa_samples or []):
        for pat in _RECORD_COUNT_PATTERNS:
            m = pat.search(sample)
            if m:
                try:
                    candidates.append(int(m.group(1).replace(",", "")))
                    break
                except (ValueError, IndexError):
                    pass
    if candidates:
        return max(candidates)
    head = (markdown_excerpt or "")[:800]
    for pat in _RECORD_COUNT_PATTERNS:
        m = pat.search(head)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except (ValueError, IndexError):
                continue
    return None


def _infer_title(markdown_excerpt: str, url: str) -> str:
    """Pick a human-readable title from markdown's first usable line, or
    fall back to the URL's last path segment cleaned up.

    Strips markdown link syntax `[text](href)` → `text`, leading list
    bullets, and heading hashes. Bails out for the URL fallback if no
    line in the first ~10 has 3+ characters.
    """
    lines = (markdown_excerpt or "").splitlines()
    for raw_line in lines[:15]:
        line = raw_line.strip().lstrip("#*->•·\t ").strip()
        if not line or len(line) < 3:
            continue
        # Markdown link → text
        m_link = re.match(r"\[([^\]]+)\]\([^)]*\)", line)
        if m_link:
            text = m_link.group(1).strip()
            if len(text) >= 3:
                return text[:120]
        # Image-only line — skip (e.g. "![](https://...)")
        if line.startswith("!["):
            continue
        return line[:120]
    # Fallback: last meaningful URL segment
    try:
        from urllib.parse import urlparse  # local import — used rarely
        path = urlparse(url).path.rstrip("/")
        seg = path.rsplit("/", 1)[-1] if "/" in path else path
        if not seg:
            seg = urlparse(url).netloc
        return seg.replace("-", " ").replace("_", " ").strip()[:120]
    except Exception:
        return ""


def _to_data_page_node_template(
    crawl_node: dict[str, Any], depth: int = 0,
) -> dict[str, Any] | None:
    """Recursively convert a crawl_list_tree node → DataPageNode-shaped dict.

    Returns None for nodes the agent should NOT emit (error / skipped /
    already-visited). The agent copies what we return into
    `portal_tree.root` verbatim, only adding `fields_available` (its
    semantic inference of which schema fields the page exposes) and
    `tree_summary` (the narrative paragraph).

    Field mapping:
      page_kind="list" → page_type="list"
      page_kind="leaf" → page_type="detail"   ← key remap
      page_kind="error"/"skipped" → dropped (not emittable)

    Inferred fields:
      title         from markdown_excerpt first usable line, URL fallback
      record_count  from spa_list_text_samples + markdown header scan
      is_sampled    always True for emittable nodes (we visited them)
    """
    kind = crawl_node.get("page_kind")
    if kind in ("error", "skipped"):
        return None

    excerpt = crawl_node.get("markdown_excerpt") or ""
    url = crawl_node.get("url") or ""
    list_signals = crawl_node.get("list_signals") or {}
    spa_samples = list_signals.get("spa_list_text_samples") or []

    template: dict[str, Any] = {
        "url": url,
        "page_type": "detail" if kind == "leaf" else "list",
        "title": _infer_title(excerpt, url),
        "depth": depth,
        "is_sampled": True,
        "record_count": _infer_record_count(spa_samples, excerpt),
        # Session-relative path to the full markdown on disk. Agent reads
        # via Read(markdown_path) when the excerpt isn't enough, or
        # Grep(pattern, glob="crawled/<csi>/*.md") for batched searches.
        "markdown_path": crawl_node.get("markdown_path", ""),
        # Agent fills these two — they require semantic understanding the
        # tool can't do without an LLM call.
        "fields_available": [],
        "children": [],
    }

    for child in (crawl_node.get("children") or []):
        child_tpl = _to_data_page_node_template(child, depth + 1)
        if child_tpl is not None:
            template["children"].append(child_tpl)

    return template


def _extract_md_links(markdown: str, base_url: str) -> list[str]:
    """Pull all [text](href) anchors from markdown, resolve to absolute URLs."""
    out: list[str] = []
    for m in _MD_LINK_RE.finditer(markdown or ""):
        href = (m.group(2) or "").strip()
        if not href:
            continue
        try:
            absolute = urljoin(base_url, href).split("#", 1)[0]
        except Exception:
            continue
        if absolute:
            out.append(absolute)
    return out


def _prefilter_links(
    raw_links: list[str], seed_url: str,
) -> tuple[list[str], dict[str, Any]]:
    """Cut raw markdown links down to plausibly-list-item candidates.

    Layers (cheap → less cheap):
      L1 syntax     : protocol blacklist, fragment-only, static extensions
      L2 eTLD+1     : same publisher (subdomain ok — Ctrip/JD/Zhihu cross-
                      subdomain detail pages must survive this)
      L2.5 chrome   : drop CDN/forum/help subdomains
      L4 nav        : nav blacklist (/login, /pricing, /cart, ...)
      L5 dedup     : drop URLs repeated ≥ 5 times (template boilerplate)

    NOTE: L3 path-prefix filter was REMOVED — it dropped legitimate sibling
    URLs whenever the site's detail pages lived outside the seed's parent
    directory (Agoda case: seed `/zh-cn/city/...`, details at
    `/zh-cn/{slug}/hotel/...`, all dropped). Cluster_urls_by_skeleton
    downstream handles same-site noise better than a path-anchored gate.

    Returns (kept_links, stats). stats includes per-layer drop counts and a
    `dropped_hosts` map of eTLD+1 → count for L2 drops — useful for spotting
    cross-ecosystem links the agent might want to re-explore (Taobao →
    Tmall).
    """
    seed_p = urlparse(seed_url)
    seed_netloc = seed_p.netloc.lower()
    seed_etld1 = _registrable_etld1(seed_netloc)

    # Occurrence count for L5
    occ: dict[str, int] = defaultdict(int)
    for link in raw_links:
        occ[link] += 1

    dropped_hosts: dict[str, int] = defaultdict(int)
    stats: dict[str, Any] = {
        "raw_count": len(raw_links),
        "l1_syntax": 0, "l2_etld1": 0, "l2_chrome": 0,
        "l4_nav": 0, "l5_repeated": 0,
    }

    kept: list[str] = []
    seen: set[str] = set()

    for link in raw_links:
        if link in seen:
            continue
        seen.add(link)

        # L1 syntactic garbage
        if link.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
            stats["l1_syntax"] += 1
            continue
        try:
            p = urlparse(link)
        except Exception:
            stats["l1_syntax"] += 1
            continue
        if not (p.scheme or "").startswith("http"):
            stats["l1_syntax"] += 1
            continue
        path_lower = (p.path or "").lower()
        if any(path_lower.endswith(ext) for ext in _STATIC_EXT):
            stats["l1_syntax"] += 1
            continue
        if not p.path or p.path == "/":
            stats["l1_syntax"] += 1
            continue

        link_netloc = p.netloc.lower()
        link_etld1 = _registrable_etld1(link_netloc)

        # L2: eTLD+1 match (broader than strict netloc — JD `search.jd.com`
        # → `item.jd.com` must survive). Cross-eTLD+1 (taobao → tmall) is
        # captured in `dropped_hosts` so the agent can spot the leak.
        if link_etld1 != seed_etld1:
            stats["l2_etld1"] += 1
            dropped_hosts[link_etld1] += 1
            continue

        # L2.5: chrome subdomain blacklist
        if _CHROME_SUBDOMAIN_RE.match(link_netloc):
            stats["l2_chrome"] += 1
            continue

        # L3 path-prefix filter intentionally REMOVED — see docstring.

        # L4: nav blacklist
        if _NAV_PATH_RE.match(p.path):
            stats["l4_nav"] += 1
            continue

        # L5: boilerplate repetition
        if occ[link] >= 5:
            stats["l5_repeated"] += 1
            continue

        kept.append(link)

    stats["unique_count"] = len(seen)
    stats["kept_count"] = len(kept)
    stats["dropped_hosts"] = dict(dropped_hosts)
    return kept, stats


def _is_list_page(
    markdown: str, prefiltered_links: list[str],
) -> tuple[bool, dict[str, Any]]:
    """Heuristic decision: is this fetched page a list page?

    True if ANY of:
      - link_count ≥ 10 AND top-3 clusters cover ≥ 70% of them
        (homogeneous link structure → enumeration UI)
      - ≥ 2 list-keyword matches in markdown ("Showing N results", "Filter by")
      - ≥ 2 pagination links visible
      - ≥ 1 SPA-list text signal in markdown body (catches JS-rendered
        list pages like Ctrip/Booking/Agoda whose item cards are NOT in
        the static markdown but the page TEXT still says "11,264 hotels"
        / "Showing X of Y" / etc.)

    Rationale for the SPA signal: list-page-ness is a SEMANTIC property
    (the page enumerates many same-shape items). It must NOT depend on
    whether item links happen to be extractable from static HTML — that
    confuses RENDERING (SPA vs SSR) with SEMANTICS. A page that says
    "约 11,264 家酒店" semantically IS a list page even when its hotel
    cards are React components and the static markdown only shows nav.

    Otherwise treated as a leaf (terminal data resource).
    """
    md = markdown or ""
    signals: dict[str, Any] = {
        "link_count": len(prefiltered_links),
        "keyword_hits": 0,
        "pagination_link_count": 0,
        "spa_list_text_hits": 0,
        "top_cluster_share": 0.0,
        "n_clusters": 0,
        "reasons": [],
    }

    signals["keyword_hits"] = len(_LIST_KEYWORD_RE.findall(md))
    signals["pagination_link_count"] = sum(
        1 for u in prefiltered_links if _PAGINATION_PATH_RE.search(u)
    )
    # New SPA-friendly signal — catches enumeration evidence in page TEXT
    # even when JS-rendered cards leave the static markdown with only nav.
    spa_matches = _SPA_LIST_TEXT_RE.findall(md)
    signals["spa_list_text_hits"] = len(spa_matches)
    signals["spa_list_text_samples"] = spa_matches[:5]  # debug visibility

    if prefiltered_links:
        refined = _frequency_refine_skeleton(prefiltered_links, k_threshold=5)
        skeleton_counts: dict[str, int] = defaultdict(int)
        for u in prefiltered_links:
            skeleton_counts[refined.get(u, u)] += 1
        sorted_counts = sorted(skeleton_counts.values(), reverse=True)
        signals["n_clusters"] = len(skeleton_counts)
        top3 = sum(sorted_counts[:3])
        signals["top_cluster_share"] = round(
            top3 / len(prefiltered_links), 3,
        ) if prefiltered_links else 0.0

    is_list = False
    if signals["link_count"] >= 10 and signals["top_cluster_share"] >= 0.7:
        is_list = True
        signals["reasons"].append("homogeneous_links")
    if signals["keyword_hits"] >= 2:
        is_list = True
        signals["reasons"].append("list_keywords")
    if signals["pagination_link_count"] >= 2:
        is_list = True
        signals["reasons"].append("pagination_markers")
    if signals["spa_list_text_hits"] >= 1:
        is_list = True
        signals["reasons"].append("spa_list_text")

    signals["is_list"] = is_list
    return is_list, signals


def _url_hash(url: str) -> str:
    """Stable short hash for filename use."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _save_page_markdown(
    url: str, markdown: str, session_id: str,
    page_kind: str = "", depth: int = 0,
) -> str:
    """Save full markdown into the session workspace.

    Returns the **session-relative POSIX path** (e.g. ``crawled/csi-x/abc.md``)
    so the agent can pass it directly to the SDK's Read/Grep tools, or an
    empty string on failure. Also appends one record to
    ``crawled/<session_id>/_node_index.jsonl`` so cross-tool lookup
    (url → markdown_path) is cheap and the file is grep-able.

    We save synchronously (write_text) since markdown is small (<1MB typical)
    and aiofiles isn't already a dep. If this becomes a bottleneck, swap to
    asyncio.to_thread.
    """
    if not markdown:
        return ""
    try:
        session_dir = _get_session_dir()
        out_dir = _crawl_cache_dir(session_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{_url_hash(url)}.md"
        md_chars = len(markdown)
        out_path.write_text(
            f"<!-- url: {url} -->\n"
            f"<!-- page_kind: {page_kind} -->\n"
            f"<!-- depth: {depth} -->\n"
            f"<!-- markdown_chars: {md_chars} -->\n"
            f"<!-- saved_at: {_now_iso()} -->\n\n{markdown}",
            encoding="utf-8",
        )
        rel = out_path.relative_to(session_dir).as_posix()
        # Index: per-csi jsonl with url/path/kind/depth/size — fast cross-tool lookup
        # markdown_chars is critical: agent uses it to decide whether Read can
        # cover the file in one call (~80KB safe; > 100KB hits the 25K token
        # ceiling and Read errors out — must paginate or Grep instead).
        idx_path = out_dir / "_node_index.jsonl"
        try:
            with idx_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "url": url, "file_path": rel,
                    "page_kind": page_kind, "depth": depth,
                    "markdown_chars": md_chars,
                    "saved_at": _now_iso(),
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("_node_index write failed for %s: %s", url[:60], e)
        return rel
    except Exception as e:
        logger.warning("crawl_list_tree save failed for %s: %s", url[:60], e)
        return ""


async def _crawl_node(
    url: str,
    depth: int,
    max_depth: int,
    max_per_skeleton: int,
    skip_pagination: bool,
    session_id: str,
    visited: set[str],
    total_counter: list[int],
    max_total_pages: int,
    counter_lock: asyncio.Lock,
) -> dict[str, Any]:
    """Recursively crawl one node and return a tree-node dict.

    `visited` is a shared set of canonical URLs (cycle prevention).
    `total_counter` is a single-element list used as a mutable counter
    that all recursive calls share (since we can't pass nonlocal-int).
    `counter_lock` is an asyncio.Lock that protects the visited + counter
    read-modify-write — needed because the cluster loop below now
    `asyncio.gather`s children in parallel, so multiple `_crawl_node`
    coroutines can race on the budget check + increment.
    """
    canon = _safe_canon_for_crawl(url)

    # Atomic: check budget + cycle, then claim a slot
    async with counter_lock:
        if canon and canon in visited:
            return {
                "url": url, "depth": depth, "page_kind": "skipped",
                "skipped_reason": "already_visited", "children": [],
            }
        if total_counter[0] >= max_total_pages:
            return {
                "url": url, "depth": depth, "page_kind": "skipped",
                "skipped_reason": "budget_exhausted", "children": [],
            }
        if canon:
            visited.add(canon)
        total_counter[0] += 1

    # Fetch page (reuses firecrawl/jina/httpx fallback chain). want_html=False:
    # the crawl only ever used the markdown — fetching rawHtml too meant every
    # in-flight node buffered a second multi-MB copy of the page for nothing.
    # The fan-out slot is held for the FETCH only (released before recursion),
    # so the recursion below can never deadlock on the global budget.
    try:
        async with _get_fanout_sem():
            _raw_html, markdown = await fetch_page_with_html(url, want_html=False)
    except Exception as e:
        return {
            "url": url, "depth": depth, "page_kind": "error",
            "error": f"{type(e).__name__}: {e}", "children": [],
        }

    if not markdown:
        return {
            "url": url, "depth": depth, "page_kind": "error",
            "error": "empty_content", "children": [],
        }

    raw_links = _extract_md_links(markdown, url)
    kept, prefilter_stats = _prefilter_links(raw_links, url)

    is_list, list_signals = _is_list_page(markdown, kept)
    page_kind_str = "list" if is_list else "leaf"

    # Save markdown AFTER classification so page_kind/depth get baked into
    # the file frontmatter + _node_index.jsonl. md_path is session-relative
    # POSIX (e.g. "crawled/csi-x/abc.md") — agent passes it to Read/Grep.
    md_path = _save_page_markdown(
        url, markdown, session_id,
        page_kind=page_kind_str, depth=depth,
    )

    node: dict[str, Any] = {
        "url": url,
        "depth": depth,
        "page_kind": page_kind_str,
        "is_list_page": is_list,
        "markdown_path": md_path,
        "markdown_chars": len(markdown),
        "markdown_excerpt": markdown[:1500],
        "links_total": len(raw_links),
        "links_kept": len(kept),
        "prefilter_stats": prefilter_stats,
        "list_signals": list_signals,
        "children": [],
    }

    # Per-node run-log event — captures the decision for THIS visit.
    # All dropped hosts saved in full (was top-5 only) so cross-eTLD+1
    # leaks (taobao→tmall etc.) are fully traceable.
    log_event("crawl_node", {
        "url": url,
        "depth": depth,
        "page_kind": node["page_kind"],
        "is_list": is_list,
        "reasons": list_signals.get("reasons", []),
        "links_total": len(raw_links),
        "links_kept": len(kept),
        "links_raw": raw_links,
        "links_kept_list": kept,
        "top_cluster_share": list_signals.get("top_cluster_share"),
        "n_clusters": list_signals.get("n_clusters"),
        "markdown_chars": len(markdown),
        "markdown_path": md_path,
        "prefilter_stats": prefilter_stats,
    })

    # Release the page text before any recursion below: children can crawl
    # for minutes, and holding every ancestor's full markdown alive
    # multiplied resident memory by tree depth (it's already on disk at
    # md_path; the node keeps only the 1500-char excerpt).
    del markdown, raw_links

    # Stop conditions
    if not is_list:
        return node
    if depth >= max_depth:
        node["stopped_reason"] = "max_depth_reached"
        log_event("crawl_stop", {"url": url, "depth": depth, "reason": "max_depth_reached"})
        return node
    if total_counter[0] >= max_total_pages:
        node["stopped_reason"] = "budget_exhausted"
        log_event("crawl_stop", {"url": url, "depth": depth, "reason": "budget_exhausted"})
        return node

    # Cluster the kept links, sample length-diverse per cluster, recurse
    refined = _frequency_refine_skeleton(kept, k_threshold=5)
    clusters: dict[str, list[str]] = defaultdict(list)
    for u in kept:
        clusters[refined.get(u, u)].append(u)

    cluster_list = sorted(clusters.items(), key=lambda kv: -len(kv[1]))

    # Build sample list across all clusters first (still skipping pagination),
    # then asyncio.gather them. Children no longer execute serially — each
    # child's budget claim is protected by counter_lock inside _crawl_node,
    # so any sample that races past the budget will return a skipped node.
    sample_plan: list[tuple[str, str]] = []   # (skeleton, sample_url)
    for skeleton, urls_in_cluster in cluster_list:
        if skip_pagination and _PAGINATION_PATH_RE.search(skeleton):
            continue
        for sample_url in _sample_length_diverse(urls_in_cluster, max_per_skeleton):
            sample_plan.append((skeleton, sample_url))

    async def _crawl_child(skel: str, sample_url: str) -> dict[str, Any]:
        c = await _crawl_node(
            sample_url, depth + 1, max_depth, max_per_skeleton,
            skip_pagination, session_id, visited, total_counter,
            max_total_pages, counter_lock,
        )
        c["from_skeleton"] = skel
        return c

    if sample_plan:
        # Per-level cap: at most _CRAWL_CHILD_CONCURRENCY subtrees of THIS
        # node expand at once (the slot is held across the child's whole
        # subtree, so breadth is bounded at every level of the recursion).
        # The global fan-out budget separately bounds actual in-flight
        # fetches across the entire process.
        level_sem = asyncio.Semaphore(_CRAWL_CHILD_CONCURRENCY)

        async def _crawl_child_bounded(skel: str, su: str) -> dict[str, Any]:
            async with level_sem:
                return await _crawl_child(skel, su)

        children: list[dict[str, Any]] = list(await asyncio.gather(
            *[_crawl_child_bounded(skel, su) for (skel, su) in sample_plan]
        ))
    else:
        children = []

    node["children"] = children
    if total_counter[0] >= max_total_pages and len(children) < sum(
        min(max_per_skeleton, len(v)) for k, v in cluster_list
        if not (skip_pagination and _PAGINATION_PATH_RE.search(k))
    ):
        node["stopped_reason"] = "budget_exhausted"
    return node


def _safe_canon_for_crawl(url: str) -> str:
    """Best-effort canonicalize URL for cycle detection. Falls back to
    lowercased + stripped-slash if the canonicalizer errors."""
    if not url:
        return ""
    try:
        return _canonicalize(url)
    except Exception:
        return url.strip().lower().rstrip("/")


def _collect_node_index_by_path(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Walk a crawl tree and return {markdown_path: node_ref} for every
    node that has a saved markdown file.

    Values are references to the live dicts in the tree, so mutating
    them (e.g. adding `helper_classification`) updates the tree in place.
    """
    idx: dict[str, dict[str, Any]] = {}
    if not isinstance(node, dict):
        return idx
    path = node.get("markdown_path") or ""
    if path:
        idx[path] = node
    for child in (node.get("children") or []):
        idx.update(_collect_node_index_by_path(child))
    return idx


async def _auto_classify_tree(root: dict[str, Any]) -> dict[str, int]:
    """Run classify_urls on every saved-markdown node in `root` and graft
    the helper output onto each node as `helper_classification`.

    `task_hint` is sourced AUTOMATICALLY from the bound RunLogger's
    `query_text` (i.e. the user's original `/discover` query). This means
    crawl_list_tree / firecrawl_map callers don't need to pass any task
    context — it's derived from the run's pre-existing user-query
    context. If no RunLogger is bound (e.g. unit tests), task_hint is
    empty and classify_urls falls back to structural-only judgment.

    Returns a small stats dict {n_classified, n_failed, n_not_relevant}
    that the caller can include in tool_result signals.
    """
    node_idx = _collect_node_index_by_path(root)
    if not node_idx:
        return {"n_classified": 0, "n_failed": 0, "n_not_relevant": 0}
    paths = list(node_idx.keys())

    # Task context priority: main-LLM-authored task_description.md
    # (preferred — written as MANDATORY first step) → fallback to the raw
    # /discover query stored on the bound RunLogger when the description
    # file is absent (e.g. unit-test path or pre-mandatory-step flow).
    task_hint = _read_task_description()
    if not task_hint:
        rl = get_run_logger()
        task_hint = (getattr(rl, "query_text", "") or "").strip() if rl else ""

    try:
        env = await classify_urls.handler({
            "node_paths": paths,
            "task_hint": task_hint,
        })
        payload = json.loads(env["content"][0]["text"])
    except Exception as e:
        logger.warning("auto-classify failed: %s", e)
        return {"n_classified": 0, "n_failed": len(paths), "n_not_relevant": 0}
    if "error" in payload:
        logger.warning("auto-classify returned error: %s", payload["error"])
        return {"n_classified": 0, "n_failed": len(paths), "n_not_relevant": 0}
    classifications = payload.get("classifications") or []
    keys = ("types", "relevance",
            "confidence", "reason", "evidence_excerpt")
    for cls in classifications:
        fp = cls.get("file_path")
        if fp and fp in node_idx:
            node_idx[fp]["helper_classification"] = {
                k: cls[k] for k in keys if k in cls
            }
    return {
        "n_classified": len(classifications),
        "n_failed": int(payload.get("n_failed", 0)),
        "n_not_relevant": int(payload.get("n_not_relevant", 0)),
    }


_CRAWL_LIST_TREE_MAX_URLS_PER_CALL = 3


async def _crawl_list_tree_one_url(
    url: str,
    max_depth: int,
    max_per_skeleton: int,
    max_total_pages: int,
    skip_pagination: bool,
    session_id: str,
) -> dict[str, Any]:
    """Crawl ONE list-page URL into a DataPageNode tree.

    Returns the per-URL payload dict (the same shape the public tool used
    to return inside its ``_ok({...})`` envelope). On hard crash returns
    ``{seed_url, error, session_id}``. No tool_call / tool_result events
    are emitted here — the outer parallel wrapper logs once for the
    aggregate. Per-URL ``tool_error`` events ARE still emitted because
    they carry diagnostic value the aggregate can't reconstruct.
    """
    visited: set[str] = set()
    total_counter = [0]
    counter_lock = asyncio.Lock()
    try:
        root = await _crawl_node(
            url=url,
            depth=0,
            max_depth=max_depth,
            max_per_skeleton=max_per_skeleton,
            skip_pagination=skip_pagination,
            session_id=session_id,
            visited=visited,
            total_counter=total_counter,
            max_total_pages=max_total_pages,
            counter_lock=counter_lock,
        )
    except Exception as e:
        logger.exception("crawl_list_tree crashed on %s: %s", url[:80], e)
        log_event("tool_error", {
            "tool": "crawl_list_tree", "url": url,
            "error": f"{type(e).__name__}: {e}",
        })
        return {
            "seed_url": url,
            "session_id": session_id,
            "error": f"crawl failed: {type(e).__name__}: {e}",
        }

    def _walk(n: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
        kinds: dict[str, int] = defaultdict(int)
        stop_reasons: list[str] = []
        kinds[n.get("page_kind", "unknown")] += 1
        if n.get("stopped_reason"):
            stop_reasons.append(n["stopped_reason"])
        for c in n.get("children") or []:
            child_kinds, child_stops = _walk(c)
            for k, v in child_kinds.items():
                kinds[k] += v
            stop_reasons.extend(child_stops)
        return kinds, stop_reasons

    kinds_count_dd, stop_reasons = _walk(root)
    kinds_count = dict(kinds_count_dd)
    logger.info(
        "agentic.crawl_list_tree url=%s session=%s visited=%d kinds=%s",
        url[:80], session_id, total_counter[0], kinds_count,
    )

    # Auto-classify every saved-markdown node via classify_urls (helper LLM).
    # Mutates the tree in place — each node with markdown_path gets a
    # `helper_classification` dict.
    helper_stats = await _auto_classify_tree(root)

    template = _to_data_page_node_template(root)
    _save_template_for_session(session_id, template)
    root_signals = root.get("list_signals") or {}
    return {
        "seed_url": url,
        "session_id": session_id,
        "cache_dir": _crawl_cache_dir(session_id).relative_to(_get_session_dir()).as_posix(),
        "total_pages_visited": total_counter[0],
        "max_total_pages_budget": max_total_pages,
        "page_kind_counts": kinds_count,
        "stop_reasons": stop_reasons,
        "root": root,
        "data_page_node_template": template,
        "helper_classify_stats": helper_stats,
        "evidence_for_summary": {
            "total_pages_visited": total_counter[0],
            "child_kinds_observed": [
                c.get("page_kind") for c in (root.get("children") or [])
            ],
            "page_kind_counts": kinds_count,
            "links_total": root.get("links_total"),
            "links_kept": root.get("links_kept"),
            "spa_list_text_samples": root_signals.get("spa_list_text_samples") or [],
            "stop_reasons": stop_reasons,
        },
    }


@tool(
    "crawl_list_tree",
    "Recursively crawl one or more list pages — runs them in parallel "
    f"(up to {_CRAWL_LIST_TREE_MAX_URLS_PER_CALL} per call). Each URL "
    "renders pages via Firecrawl/Playwright (so query filters ?search= / "
    "?q= take effect), extracts links, prefilters chrome/CDN/nav, clusters "
    "by skeleton, samples length-diverse URLs per cluster, recurses on "
    "list nodes, stops at leaves or budget. Saves markdown to "
    "crawled/<session>/<hash>.md. NOTE: heavy operation — underlying "
    "firecrawl scrape pool is Semaphore(8), so multiple parallel crawls "
    "share that pool; wall-time gains are modest. Returns "
    "{n_urls, n_failed, results: [per-URL]} where each per-URL entry has "
    "{seed_url, session_id, root, page_kind_counts, total_pages_visited, "
    "data_page_node_template, evidence_for_summary, helper_classify_stats} "
    "on success or {seed_url, error, session_id} on per-URL failure. "
    "Every saved-markdown node carries `helper_classification` "
    "{types, relevance, confidence, reason, "
    "evidence_excerpt} grafted by the auto-classify helper (task_hint "
    "auto-sourced from the user's /discover query via the bound RunLogger).",
    {
        "urls": list,
        "max_depth": int,
        "max_per_skeleton": int,
        "max_total_pages": int,
        "skip_pagination": bool,
        "session_id_prefix": str,
    },
)
async def crawl_list_tree(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("crawl_list_tree", args)
    gate = _check_task_description()
    if gate is not None:
        return gate

    # Accept legacy {"url": str, "session_id": str} arg shape.
    raw_urls = args.get("urls")
    if not raw_urls and args.get("url"):
        raw_urls = [args.get("url")]
    if not isinstance(raw_urls, list) or not raw_urls:
        return _err("urls must be a non-empty list of strings")
    urls = [
        str(u).strip() for u in raw_urls if str(u).strip()
    ][:_CRAWL_LIST_TREE_MAX_URLS_PER_CALL]
    if not urls:
        return _err("urls contained no valid entries")

    max_depth = int(args.get("max_depth") or 3)
    max_per_skeleton = max(1, int(args.get("max_per_skeleton") or 2))
    max_total_pages = max(1, int(args.get("max_total_pages") or 30))
    skip_pagination = bool(args.get("skip_pagination", True))

    # Legacy single-URL callers may pass session_id directly; new
    # multi-URL callers pass session_id_prefix to derive per-URL ids.
    legacy_session_id = (args.get("session_id") or "").strip()
    session_id_prefix = (args.get("session_id_prefix") or "").strip()

    def _session_id_for(i: int, url: str) -> str:
        if len(urls) == 1 and legacy_session_id:
            return legacy_session_id
        if session_id_prefix:
            return f"{session_id_prefix}-{i}"
        return f"adhoc-{uuid4().hex[:8]}"

    per_url = await asyncio.gather(*[
        _crawl_list_tree_one_url(
            url=u,
            max_depth=max_depth,
            max_per_skeleton=max_per_skeleton,
            max_total_pages=max_total_pages,
            skip_pagination=skip_pagination,
            session_id=_session_id_for(i, u),
        )
        for i, u in enumerate(urls)
    ])

    n_urls = len(per_url)
    n_failed = sum(1 for r in per_url if r.get("error"))
    total_pages = sum(int(r.get("total_pages_visited") or 0) for r in per_url)
    agg_kinds: dict[str, int] = defaultdict(int)
    for r in per_url:
        for k, v in (r.get("page_kind_counts") or {}).items():
            agg_kinds[k] += int(v)
    agg_helper_failed = sum(
        int((r.get("helper_classify_stats") or {}).get("n_failed") or 0)
        for r in per_url
    )
    all_stop_reasons = [
        sr for r in per_url for sr in (r.get("stop_reasons") or [])
    ]

    signals: list[str] = []
    if n_failed == n_urls:
        signals.append("all_failed")
    elif n_failed:
        signals.append(f"partial_failures:{n_failed}")
    if "budget_exhausted" in all_stop_reasons:
        signals.append("budget_exhausted")
    if agg_kinds.get("leaf", 0) == 0 and n_failed < n_urls:
        signals.append("zero_leaves")
    if agg_kinds.get("error", 0) > 0:
        signals.append("partial_errors")
    if agg_helper_failed > 0:
        signals.append(f"auto_classify_failures:{agg_helper_failed}")

    _log_tool_result("crawl_list_tree", {
        "n_urls": n_urls,
        "n_failed": n_failed,
        "total_pages_visited": total_pages,
        "page_kind_counts": dict(agg_kinds),
        "per_url_summary": [
            {
                "seed_url": r.get("seed_url"),
                "session_id": r.get("session_id"),
                "total_pages_visited": r.get("total_pages_visited"),
                "page_kind_counts": r.get("page_kind_counts"),
                "error": r.get("error"),
            }
            for r in per_url
        ],
    }, signals=signals or None)

    return _ok({
        "n_urls": n_urls,
        "n_failed": n_failed,
        "results": per_url,
    })


# ──────────────────────────────────────────────────────────────────────
# Emit-as-you-go commit tools
#
# These tools let the agent commit discoveries (flat sources and portal_
# trees) INCREMENTALLY during the loop, instead of accumulating in
# memory and emitting one final JSON blob at end. This solves the
# "context decay" failure: agent's portal_tree.children gets re-narrated
# from memory at end-of-conversation, losing detail URLs the crawl
# actually visited.
#
# Dedup is also tool-mediated (not runner-hidden): each commit_* validates
# against earlier commits and rejects with structured error if there's
# a conflict, giving the agent immediate feedback.
# ──────────────────────────────────────────────────────────────────────


@tool(
    "check_url_committed_status",
    "Look up whether a URL has already been seen by emit-as-you-go state. "
    "Returns where (if anywhere) the URL appears: in a committed "
    "portal_tree, in a committed flat source, or in a crawl_list_tree / "
    "firecrawl_map call from earlier in this run. Read-only — does not "
    "modify state.",
    {"url": str},
)
async def check_url_committed_status(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("check_url_committed_status", args)
    url = (args.get("url") or "").strip()
    if not url:
        return _err("empty url")

    canon = _safe_canon_for_commit(url)
    in_tree = _scan_session_trees_for_url(canon)
    in_source = _scan_session_sources_for_url(canon)
    in_crawl = _lookup_url_in_crawl_tool_calls(canon)

    # Compute recommendation
    if in_tree:
        rec = (
            f"already_in_tree:csi={in_tree.get('csi')!r} — do NOT commit as "
            f"flat source (tree wins); reference this tree's data instead"
        )
    elif in_crawl:
        rec = (
            f"was_crawled_as_list_or_portal:tool={in_crawl.get('tool')!r} "
            f"session_id={in_crawl.get('session_id')!r} — use commit_portal_"
            f"tree(crawl_session_id={in_crawl.get('session_id')!r}), NOT "
            f"commit_source"
        )
    elif in_source:
        rec = (
            f"already_committed_source:index={in_source.get('source_index')} "
            f"name={in_source.get('name')!r} — to replace, call "
            f"remove_committed_source(url, reason=...) first"
        )
    else:
        rec = "ok_to_commit_source_or_portal_tree"

    summary = {
        "url": url,
        "canonical_url": canon,
        "in_committed_tree": bool(in_tree),
        "in_committed_source": bool(in_source),
        "in_crawl_tool_calls": bool(in_crawl),
    }
    _log_tool_result("check_url_committed_status", summary)
    return _ok({
        "url": url,
        "canonical_url": canon,
        "found_in_committed_tree": in_tree,        # dict or None
        "found_in_committed_source": in_source,    # dict or None
        "found_in_crawl_tool_calls": in_crawl,     # dict or None
        "recommendation": rec,
    })


# access_level values that mean "the user needs to obtain a key" — anything
# other than `open`. A keyed API with no signup_url leaves the user no way to
# get the key, so commit_source soft-warns (see _keyed_api_missing_signup).
_KEYED_ACCESS_LEVELS = {"free_reg", "api_key_free", "api_key_paid", "oauth", "paywall", "unknown"}


def _keyed_api_missing_signup(src: dict[str, Any]) -> bool:
    """True when an api source needs a key (access_level != open) but carries no
    signup_url anywhere (top-level / api_spec / metadata)."""
    types = src.get("source_type")
    types = types if isinstance(types, list) else [types]
    if "api" not in [str(t).lower() for t in types if t]:
        return False
    if str(src.get("access_level") or "unknown").lower() not in _KEYED_ACCESS_LEVELS:
        return False
    signup = (
        src.get("signup_url")
        or (src.get("api_spec") or {}).get("signup_url")
        or (src.get("metadata") or {}).get("signup_url")
    )
    return not str(signup or "").strip()


def _stamp_field_notes(record: dict[str, Any]) -> None:
    """Server-stamp `at` (UTC) + `status` onto each metadata.field_notes entry,
    in place. fill→applied, propose→pending_review. Drops malformed entries
    (missing field / bad action / propose with no reason) — provenance is
    best-effort audit metadata and must NEVER block or crash a commit."""
    meta = record.get("metadata")
    if not isinstance(meta, dict):
        return
    notes = meta.get("field_notes")
    if not isinstance(notes, list):
        return
    cleaned: list[dict[str, Any]] = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        action = n.get("action")
        if not str(n.get("field") or "").strip() or action not in ("fill", "propose"):
            continue
        if action == "propose" and not str(n.get("reason") or "").strip():
            continue  # a correction proposal without a reason is dropped
        n["at"] = _now_iso()
        n["status"] = "pending_review" if action == "propose" else "applied"
        cleaned.append(n)
    meta["field_notes"] = cleaned


@tool(
    "commit_source",
    "Append a DataSource record to this session's sources.jsonl. The "
    "`source` argument is the full DataSource dict (url, name, "
    "source_type, description, metadata, etc.) matching the schema in "
    "the system prompt. Enforced dedup — rejects with structured error "
    "when: (a) URL is already inside a committed portal_tree, (b) URL "
    "is already a committed source, (c) URL was crawled by "
    "crawl_list_tree / firecrawl_map. Error envelope includes a "
    "`recommended_action` field.",
    {"source": dict},
)
async def commit_source(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("commit_source", args)
    src = args.get("source")
    if not isinstance(src, dict):
        return _err("source must be a dict — pass the full DataSource record")
    url = (src.get("url") or "").strip()
    if not url:
        return _err("source.url is required")
    # Soft-require core fields so we don't accept obvious junk; missing
    # name/source_type/description in a flat source defeats downstream judge.
    def _field_present(f: str) -> bool:
        v = src.get(f)
        if isinstance(v, (list, tuple)):  # source_type may be a list of categories
            return len(v) > 0
        return bool(str(v or "").strip())

    missing_required = [
        f for f in ("name", "source_type", "description")
        if not _field_present(f)
    ]
    if missing_required:
        return _err(
            f"source missing required fields: {missing_required}",
            required_fields=["url", "name", "source_type", "description"],
        )

    canon = _safe_canon_for_commit(url)

    # ── Dedup rule 1: already covered by a committed portal_tree (tree wins) ──
    if existing := _scan_session_trees_for_url(canon):
        _log_tool_result("commit_source", {
            "url": url, "outcome": "rejected_covered_by_tree",
            "tree_csi": existing.get("csi"),
        }, signals=["commit_rejected"])
        return _err(
            f"url {url!r} is already covered by committed portal_tree "
            f"(tree #{existing['tree_index']}, "
            f"crawl_session_id={existing.get('csi')!r}). Tree wins — this "
            f"URL is part of that tree's structure. To reference it, point "
            f"downstream consumers at the tree, not at a duplicate flat source.",
            covered_by_tree_index=existing["tree_index"],
            covered_by_tree_csi=existing.get("csi"),
            recommended_action="skip — tree already covers this URL",
        )

    # ── Dedup rule 2: already committed as a flat source ──
    if dup := _scan_session_sources_for_url(canon):
        _log_tool_result("commit_source", {
            "url": url, "outcome": "rejected_duplicate_source",
            "duplicate_of_index": dup.get("source_index"),
        }, signals=["commit_rejected"])
        return _err(
            f"url {url!r} already committed as source #{dup['source_index']} "
            f"({dup.get('name')!r}). To replace, first call "
            f"remove_committed_source(url={url!r}, reason='...') and then "
            f"commit_source again.",
            duplicate_of_index=dup["source_index"],
            duplicate_of_name=dup.get("name"),
            recommended_action="remove_committed_source then re-commit",
        )

    # ── Dedup rule 3: URL was crawled as list/portal — anti-pattern ──
    if crawled := _lookup_url_in_crawl_tool_calls(canon):
        _log_tool_result("commit_source", {
            "url": url, "outcome": "rejected_was_crawled",
            "crawled_by": crawled.get("tool"),
            "crawl_session_id": crawled.get("session_id"),
        }, signals=["commit_rejected", "anti_pattern_portal_as_source"])
        return _err(
            f"url {url!r} was crawled as a list/portal page (tool="
            f"{crawled.get('tool')!r}, session_id="
            f"{crawled.get('session_id')!r}). Emit it as portal_tree via "
            f"commit_portal_tree(crawl_session_id="
            f"{crawled.get('session_id')!r}, ...), NOT as flat source.",
            should_use="commit_portal_tree",
            crawl_session_id=crawled.get("session_id"),
            recommended_action="commit_portal_tree with this crawl_session_id",
        )

    # ── All checks passed: persist ──
    record = dict(src)  # shallow copy
    record["canonical_url"] = canon
    record["_committed_event"] = "commit_source"
    # Stamp provenance notes server-side (trusted `at` + `status`) before write.
    _stamp_field_notes(record)
    path = _session_jsonl("sources.jsonl")
    if not _append_to_jsonl(path, record):
        _log_tool_result("commit_source", {
            "url": url, "outcome": "write_failed",
        }, signals=["commit_write_failed"])
        return _err(
            f"failed to persist source to {path}; transient I/O error — retry",
            recoverable=True,
        )

    log_event("commit_source.committed", {
        "url": url, "canonical_url": canon, "name": src.get("name"),
        "source_type": src.get("source_type"),
        "description": (src.get("description") or "")[:400],
        "provider": src.get("provider"),
        "domain": src.get("domain"),
        "tags": src.get("tags") or [],
        "data_format": src.get("data_format") or [],
        "geographic_coverage": src.get("geographic_coverage") or [],
        "temporal_coverage": src.get("temporal_coverage"),
        "update_frequency": src.get("update_frequency"),
        "access_level": src.get("access_level"),
        "license": src.get("license"),
        "discovery_method": src.get("discovery_method"),
        "evidence": ((src.get("metadata") or {}).get("evidence") or "")[:500],
    })
    _log_tool_result("commit_source", {
        "url": url, "outcome": "committed", "canonical_url": canon,
    })
    out: dict[str, Any] = {
        "committed": True,
        "url": url,
        "canonical_url": canon,
        "session_file": str(path),
    }
    # Soft warning (NOT a rejection): a keyed API with no signup_url leaves the
    # user unable to obtain the key. Nudge the agent to search_web for the
    # registration page and re-commit (remove_committed_source first).
    if _keyed_api_missing_signup(src):
        out["warning"] = (
            "this API needs a key (access_level != open) but has no signup_url — "
            "the user won't know where to get the key. search_web for "
            f"'{src.get('provider') or src.get('name')} API key signup', then "
            "remove_committed_source + commit_source again with signup_url filled."
        )
    return _ok(out)


def _annotate_template_fields(
    template: dict[str, Any],
    fields_per_url: dict[str, list[str]],
    fallback_root_fields: list[str] | None = None,
    fallback_detail_fields: list[str] | None = None,
) -> None:
    """In-place: graft agent-provided fields_available onto each node.

    Lookup priority per node:
      1. fields_per_url[canonical(url)] — explicit per-node from agent
      2. fallback_root_fields (only for the root node)
      3. fallback_detail_fields (for page_type=detail children)
      4. [] (empty list)
    """
    if not isinstance(template, dict):
        return

    def _walk(node: dict[str, Any], is_root: bool = False) -> None:
        if not isinstance(node, dict):
            return
        url = node.get("url") or ""
        canon = _safe_canon_for_commit(url)
        explicit = fields_per_url.get(canon) or fields_per_url.get(url)
        if explicit:
            node["fields_available"] = list(explicit)
        elif is_root and fallback_root_fields:
            node["fields_available"] = list(fallback_root_fields)
        elif node.get("page_type") == "detail" and fallback_detail_fields:
            node["fields_available"] = list(fallback_detail_fields)
        # else: leave whatever was there (template default is [])
        for child in (node.get("children") or []):
            _walk(child, is_root=False)

    _walk(template, is_root=True)


def _count_descendants(node: dict[str, Any]) -> int:
    """Total nodes including root."""
    if not isinstance(node, dict):
        return 0
    n = 1
    for c in (node.get("children") or []):
        n += _count_descendants(c)
    return n


def _count_kind(node: dict[str, Any], page_type: str) -> int:
    """Count nodes (anywhere in tree) with given page_type."""
    if not isinstance(node, dict):
        return 0
    n = 1 if node.get("page_type") == page_type else 0
    for c in (node.get("children") or []):
        n += _count_kind(c, page_type)
    return n


@tool(
    "commit_portal_tree",
    "Commit a structured hub-and-children portal as a DataPageTree. "
    "Use when the crawl revealed real layered structure — a list/hub "
    "root with detail children that share a schema. The tree shape "
    "preserves the parent-child mapping and per-level fields, which "
    "flat sources can't express. For single-page hubs or crawls whose "
    "root has 0 structural children, commit_source captures the same "
    "content without the empty tree wrapper. Mechanically: the tool "
    "reconstructs the tree structure from the run log using "
    "crawl_session_id and grafts your semantic annotations on top. "
    "Required inputs: crawl_session_id (from the crawl_list_tree / "
    "firecrawl_map return payload), tree_summary, fields_available_root, "
    "fields_available_detail. Optional: fields_available_per_node "
    "({url: [field,...]}) and root_title_override. Side effect: if any "
    "URL in this tree's descendants is already a committed flat source, "
    "that source is auto-tombstoned; the tool response lists the "
    "superseded source URLs.",
    {
        "crawl_session_id": str,
        "tree_summary": str,
        "fields_available_root": list,        # list[str] — fields on root list page
        "fields_available_detail": list,      # list[str] — fields on a sample detail page
        "fields_available_per_node": dict,    # optional override: {url: [field,...]}
        "root_title_override": str,           # optional: replace inferred title
    },
)
async def commit_portal_tree(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("commit_portal_tree", args)
    csi = (args.get("crawl_session_id") or "").strip()
    tree_summary = (args.get("tree_summary") or "").strip()
    if not csi:
        return _err("crawl_session_id is required")
    if not tree_summary:
        return _err(
            "tree_summary is required — must quote ≥2 numbers from "
            "evidence_for_summary in the crawl tool result"
        )

    # ── Look up the tool result by session_id ──
    tool_data = _lookup_tool_result_by_session(csi)
    if not tool_data:
        return _err(
            f"crawl_session_id={csi!r} not found in run log — no matching "
            f"crawl_list_tree or firecrawl_map call. Call one of those "
            f"tools first; copy the session_id from its return payload.",
            recommended_action="call crawl_list_tree(url, ...) first",
        )
    summary = tool_data.get("summary") or {}
    tool_name = summary.get("tool") or tool_data.get("tool")  # may be elsewhere

    # We need the template — but _log_tool_result stores only the summary,
    # not the full return payload. So we read the data_page_node_template
    # by re-extracting from the agent's tool envelope... which we don't have
    # at runner side. SOLUTION: the tool result summary was extended to
    # include `data_page_node_template` via session-file caching.
    # FOR NOW: since we don't yet write template to summary, fall back to
    # the agent-supplied tree structure via a separate mechanism.
    #
    # Actually the cleanest path: store the template into a session-side
    # cache file when crawl_list_tree / firecrawl_map return it, keyed by
    # session_id. Then look it up here. Let me write that helper.
    template = _load_template_for_session(csi)
    if not template:
        return _err(
            f"crawl_session_id={csi!r} has no data_page_node_template "
            f"cached. crawl_list_tree always produces one; firecrawl_map "
            f"only when fallback ran. If you're using firecrawl_map without "
            f"fallback, there's no tree structure to commit — emit the "
            f"map result as a flat source instead.",
            recommended_action=(
                "If you intended a tree, call crawl_list_tree(url) and use "
                "its session_id. If sitemap-only is your data, use commit_"
                "source on the catalog URL."
            ),
        )

    # ── Apply agent semantic annotations onto tool-authored structure ──
    _annotate_template_fields(
        template,
        fields_per_url=(args.get("fields_available_per_node") or {}),
        fallback_root_fields=(args.get("fields_available_root") or []),
        fallback_detail_fields=(args.get("fields_available_detail") or []),
    )
    if title_override := (args.get("root_title_override") or "").strip():
        template["title"] = title_override

    # ── Self-dedup: same root URL already committed as a tree? ──
    root_canon = _safe_canon_for_commit(template.get("url", ""))
    if existing := _scan_session_trees_for_url(root_canon):
        if existing.get("matched_at_root"):
            _log_tool_result("commit_portal_tree", {
                "csi": csi, "outcome": "rejected_duplicate_tree",
                "existing_tree_index": existing.get("tree_index"),
            }, signals=["commit_rejected"])
            return _err(
                f"a portal_tree with root url={template.get('url')!r} is "
                f"already committed (tree #{existing['tree_index']}, "
                f"csi={existing.get('csi')!r}). Use that one — don't commit "
                f"duplicates of the same crawl.",
                existing_tree_index=existing["tree_index"],
                existing_csi=existing.get("csi"),
            )

    # ── Side-effect: tombstone any committed sources whose URL is in this
    # tree's descendant set (tree wins). Collect first, write tombstones
    # after the tree is persisted so we don't leave orphan tombstones if
    # the tree write fails.
    tree_url_set: set[str] = set()
    _walk_node_urls(template, tree_url_set)
    overlapping: list[dict[str, Any]] = []
    for u in tree_url_set:
        if hit := _scan_session_sources_for_url(u):
            overlapping.append({"url": hit.get("url"), "source_index": hit.get("source_index")})

    # ── Build the full DataPageTree dict ──
    tree_record = {
        "root": template,
        "total_detail_pages": _count_kind(template, "detail"),
        "sampled_detail_pages": _count_kind(template, "detail"),  # all template nodes are sampled
        "field_progression": {
            "list_page": list(args.get("fields_available_root") or []),
            "detail_page": list(args.get("fields_available_detail") or []),
        },
        "crawl_session_id": csi,
        "tree_summary": tree_summary,
        "_committed_event": "commit_portal_tree",
        "_committed_via_tool": tool_name,
    }

    # Persist tree first
    trees_path = _session_jsonl("portal_trees.jsonl")
    if not _append_to_jsonl(trees_path, tree_record):
        _log_tool_result("commit_portal_tree", {
            "csi": csi, "outcome": "write_failed",
        }, signals=["commit_write_failed"])
        return _err(
            f"failed to persist tree to {trees_path}; transient I/O error",
            recoverable=True,
        )

    # Then write tombstones for overlapping sources (best-effort —
    # tombstone write failure doesn't unwind the tree commit)
    sources_path = _session_jsonl("sources.jsonl")
    tombstoned_urls: list[str] = []
    for ov in overlapping:
        ts_record = {
            "_tombstone": True,
            "tombstoned_url": ov["url"],
            "reason": "superseded_by_portal_tree",
            "by_tree_csi": csi,
            "tombstoned_source_index": ov["source_index"],
        }
        if _append_to_jsonl(sources_path, ts_record):
            tombstoned_urls.append(ov["url"])

    log_event("commit_portal_tree.committed", {
        "csi": csi,
        "root_url": template.get("url"),
        "tree_size": _count_descendants(template),
        "n_detail_nodes": tree_record["total_detail_pages"],
        "superseded_sources_count": len(tombstoned_urls),
    })
    _log_tool_result("commit_portal_tree", {
        "csi": csi, "outcome": "committed",
        "root_url": template.get("url"),
        "tree_size": _count_descendants(template),
        "superseded_sources_count": len(tombstoned_urls),
    })
    return _ok({
        "committed": True,
        "crawl_session_id": csi,
        "tree_size_nodes": _count_descendants(template),
        "n_detail_nodes": tree_record["total_detail_pages"],
        "superseded_sources": tombstoned_urls,    # URL list per decision Q1=A
        "session_file": str(trees_path),
    })


@tool(
    "remove_committed_source",
    "Retract a previously-committed flat source by writing a tombstone "
    "record. `reason` is required and is written into both the tombstone "
    "record and the run log. After tombstoning, commit_source on the same "
    "URL succeeds (writes a new record with the new metadata).",
    {"url": str, "reason": str},
)
async def remove_committed_source(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("remove_committed_source", args)
    url = (args.get("url") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not url:
        return _err("url is required")
    if not reason:
        return _err(
            "reason is required (1+ sentence explaining why this source "
            "should be removed) — used for audit",
        )
    canon = _safe_canon_for_commit(url)
    existing = _scan_session_sources_for_url(canon)
    if not existing:
        return _err(
            f"no live committed source found with url={url!r} (it may "
            f"already be tombstoned, or never committed). To verify, call "
            f"check_url_committed_status(url={url!r}).",
            recommended_action="check_url_committed_status",
        )
    path = _session_jsonl("sources.jsonl")
    if not _append_to_jsonl(path, {
        "_tombstone": True,
        "tombstoned_url": url,
        "reason": reason,
        "tombstoned_source_index": existing.get("source_index"),
    }):
        return _err(f"failed to write tombstone to {path}", recoverable=True)

    log_event("remove_committed_source.removed", {
        "url": url, "reason": reason,
        "source_index": existing.get("source_index"),
    })
    _log_tool_result("remove_committed_source", {
        "url": url, "outcome": "tombstoned",
        "source_index": existing.get("source_index"),
        "reason_len": len(reason),
    })
    return _ok({
        "removed": True,
        "url": url,
        "canonical_url": canon,
        "tombstoned_source_index": existing.get("source_index"),
    })


# ──────────────────────────────────────────────────────────────────────
# Template caching for commit_portal_tree
#
# crawl_list_tree and firecrawl_map produce `data_page_node_template`
# in their return payload, but _log_tool_result only stores the summary
# (template would be too big). To let commit_portal_tree reconstruct the
# tree later, we cache the template separately keyed by session_id under
# the session dir.
# ──────────────────────────────────────────────────────────────────────


def _save_template_for_session(session_id: str, template: dict[str, Any] | None) -> None:
    """Persist a data_page_node_template to disk so commit_portal_tree can
    re-load it. No-op if template is None/empty."""
    if not session_id or not template:
        return
    cache_dir = _get_session_dir() / "templates"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{session_id}.json").write_text(
            json.dumps(template, ensure_ascii=False, default=_default_json),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(
            "template cache write failed for session_id=%s: %s",
            session_id, e,
        )


def _load_template_for_session(session_id: str) -> dict[str, Any] | None:
    """Load the cached data_page_node_template for a session_id, or None
    if not found / unreadable."""
    if not session_id:
        return None
    path = _get_session_dir() / "templates" / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(
            "template cache read failed for session_id=%s: %s",
            session_id, e,
        )
        return None


# ──────────────────────────────────────────────────────────────────────
# classify_urls helper — batch-classify saved markdown nodes
#
# Architecture:
#   - Main LLM calls classify_urls(node_paths=[...], task_hint="...") after
#     crawl_list_tree / firecrawl_map produces a tree of saved markdown files.
#   - This tool fans out one fast LLM call PER FILE in parallel (sem=100),
#     each call sees only that one node's markdown + the static system
#     prompt below. DeepSeek prefix cache makes the system prompt nearly
#     free after the first call.
#   - Helper returns {types, relevance, confidence, reason,
#     evidence_excerpt} for each node. Main LLM uses
#     confidence to decide trust vs. verify-yourself (>0.85 trust; 0.5-0.85
#     spot-check via Read(markdown_path); <0.5 self-classify).
#
# Relevance bar is INTENTIONALLY LOW: helper returns "relevant" by default,
# only flags "not_relevant" on hard negative triggers (orthogonal topic /
# dead page / boilerplate / spam). This matches the project's discovery
# philosophy ("ERR ON THE SIDE OF INCLUSION").
#
# This tool is OPTIONAL — the prompt presents it alongside "classify
# yourself" (Option B) for experimental comparison. Main LLM remains the
# final decision maker; classify_urls is convenience + speed + batch
# consistency, not a replacement for main-LLM judgment.
# ──────────────────────────────────────────────────────────────────────


class NodeClassification(BaseModel):
    """One classification record from the helper LLM, per URL."""

    types: list[Literal["list", "embedded", "file", "api"]]  # ALL applicable; priority api>file>embedded>list
    relevance: Literal["relevant", "not_relevant"]
    confidence: float  # 0.0-1.0; <0.5 means helper has real doubt
    reason: str  # 1-2 sentences citing markdown evidence
    evidence_excerpt: str  # ≤200 chars from the markdown supporting the decision


CLASSIFY_SYSTEM_PROMPT = """\
You triage URLs for a data-source discovery pipeline. For ONE URL's
markdown, emit a NodeClassification record.

# CLASSIFICATION — list ALL applicable types in `types` (priority order: api > file > embedded > list)

- api: URL contains /api/ /v\\d+/ pattern, OR markdown is OpenAPI/Swagger UI,
       OR response content-type is application/json, OR markdown is API
       reference docs (endpoints + curl/HTTP examples).
- file: URL ends with .csv/.json/.xlsx/.parquet/.zip/.tar.gz/.xml, OR the
        markdown is dominated by a Download button/section linking to such
        files. The page IS or DIRECTLY HOSTS a downloadable artifact.
- embedded: primary user-facing content IS the data. Dataset landing page,
            product detail with structured fields, single record page,
            forum thread that IS the data; JSON-LD @type Dataset/Product/
            Article block. The PAGE is the data unit.
- list: page enumerates many same-shape items. Triggers when ≥5 same-skeleton
        links visible, OR "Showing N results" / "约 X 家" / "<N> datasets"
        phrasing in body, OR pagination markers (?page=N, /page/N).

Put ALL applicable types in `types`, most-primary first (priority order
api > file > embedded > list). Usually one; list several when they genuinely
co-apply. Example: a dataset detail page with a Download CSV button →
types=["embedded", "file"].

# CONFIDENCE

0.0-1.0. Be conservative. < 0.5 means you have real doubt about the
classification. Don't inflate to please. Confidence reflects classification
certainty, not relevance certainty.

# RELEVANCE — default "relevant", bar is LOW

Mark "not_relevant" ONLY when ONE OR MORE of these hard signals fires:

(a) TOPIC ORTHOGONAL: page core topic is in a completely different domain
    than the task. Task "Beijing hotels" + page "wedding photographer
    signup form" → not_relevant. BUT topic-adjacent stays RELEVANT:
    task "Beijing hotels" + page "Beijing restaurants" → relevant
    (overlapping scope; downstream might still find usable signals).

(b) NO REAL CONTENT: dead-end with no domain data —
    - login wall (>50% markdown is auth form / "Sign in" buttons)
    - captcha block
    - 404 / "Page not found"
    - empty page (< 500 chars, no data signals)
    - parked domain / "Buy this domain" placeholder

(c) PURE BOILERPLATE: generic legal/policy with no domain-specific data —
    - privacy policy / ToS / cookie banner only
    - generic "About us" / contact form with no data

(d) SPAM / ADWARE: visible spam patterns, popup overlays, affiliate-link
    farm with no domain content.

If you're UNCERTAIN whether (a)-(d) apply, default to RELEVANT. Format
mismatch (HTML when task wants CSV), language mismatch (English when task
implies Chinese), scope mismatch (wider/narrower geography) — ALL stay
relevant.

# REASON + EVIDENCE_EXCERPT

`reason`: 1-2 sentences. Cite specific markdown signals you saw — e.g.
"URL ends .json AND response is OpenAPI 3.0 spec" or "page body has 'Showing
30 of 11,264 hotels' enumeration phrasing". For not_relevant, cite which of
(a)-(d) trigger fired and how.

`evidence_excerpt`: ≤200 chars verbatim from the markdown that supports
your decision. Quote signal text directly.
"""


@tool(
    "classify_urls",
    "Batch-classify saved markdown files into {list, embedded, file, api} "
    "categories + task-relevance. Fans out per-URL helper LLM calls in "
    "parallel (sem=10). Returns {n_total, n_failed, n_not_relevant, "
    "by_class, classifications} where each item in classifications is "
    "{file_path, url, types, relevance ∈ "
    "{relevant, not_relevant}, confidence, reason, evidence_excerpt}. "
    "Helper applies a LOW relevance threshold — flags not_relevant only "
    "on hard negative signals (orthogonal topic / dead page / boilerplate "
    "/ spam).",
    {
        "node_paths": list,            # ["fetched/xxx.md", "crawled/csi/yyy.md", ...]
        "task_hint": str,              # 1-sentence summary of user's discovery task
        "criterion_override": str,     # optional extra criterion you want helper to apply
        "model_tier": str,             # optional: "fast" or "strong"; defaults to "strong"
    },
)
async def classify_urls(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("classify_urls", args)
    node_paths_raw = args.get("node_paths") or []
    if not isinstance(node_paths_raw, list) or not node_paths_raw:
        return _err("node_paths must be a non-empty list of relative file paths")
    node_paths = [str(p).strip() for p in node_paths_raw if str(p).strip()]
    if not node_paths:
        return _err("node_paths contained no valid entries")
    task_hint = (args.get("task_hint") or "").strip()
    criterion_override = (args.get("criterion_override") or "").strip()
    model_tier = (args.get("model_tier") or "strong").strip().lower()
    if model_tier not in ("fast", "strong"):
        model_tier = "strong"

    session_dir = _get_session_dir()
    # Concurrency cap per operator decision 2026-06-10: 10 (was 100). The
    # provider key tolerates far more, but 100 parallel helper calls (a)
    # held up to 100 × 64KB prepared markdown bodies in flight at once and
    # (b) starved the main LLM + sibling tools of provider concurrency —
    # one of the valves behind the 31.6GB OOM.
    sem = asyncio.Semaphore(min(len(node_paths), 10))

    def _prepare_markdown(content: str, max_chars: int = 64000) -> str:
        """Helper sees full content up to 64KB. Beyond that, head 48KB + tail 16KB
        (preserves frontmatter + top layout + bottom footer signals)."""
        if len(content) <= max_chars:
            return content
        return (
            content[:48000]
            + f"\n\n[... ~{len(content) - 64000} chars omitted ...]\n\n"
            + content[-16000:]
        )

    def _extract_url_from_frontmatter(content: str) -> str:
        """Read `<!-- url: ... -->` from the file head; empty string on miss."""
        for line in content.splitlines()[:6]:
            m = re.match(r"<!--\s*url:\s*(\S+)\s*-->", line)
            if m:
                return m.group(1).strip()
        return ""

    async def _classify_one(path: str) -> dict[str, Any]:
        async with sem:
            try:
                full_path = session_dir / path
                if not full_path.exists():
                    return {
                        "file_path": path,
                        "url": "",
                        "types": ["embedded"],
                        "relevance": "relevant",
                        "confidence": 0.0,
                        "reason": "[helper_error] file not found",
                        "evidence_excerpt": "",
                    }
                markdown = full_path.read_text(encoding="utf-8", errors="ignore")
                url = _extract_url_from_frontmatter(markdown) or path

                # Phase 5: deterministic skill lookup. On a HIT classify FROM the
                # learned skill ENTRY (read the skill, NOT the page markdown) —
                # the entry is a PRIOR; the downstream harness verifies it. On a
                # MISS, fall through to the normal markdown classification.
                hit_entry = None
                hit_ref = ""
                if url.startswith("http"):
                    try:
                        patterns = await get_skill_library().lookup_by_url(url)
                    except Exception:  # noqa: BLE001
                        patterns = []
                    if patterns:
                        up = urlparse(url)
                        url_path = up.path + (f"?{up.query}" if up.query else "")
                        hit_entry = patterns[0].focused_entry(url_path)
                        hit_ref = patterns[0].pattern_id

                if hit_entry is not None:
                    prior = {
                        "types": [t.value for t in hit_entry.types],
                        "page_type": hit_entry.page_type,
                        "data_type": hit_entry.data_type,
                        "site_type": hit_entry.site_type,
                        "fields": hit_entry.fields,
                        "caveats": hit_entry.caveats,
                        "notes": hit_entry.notes,
                    }
                    user_msg = (
                        f"Task: {task_hint or '(no task hint provided)'}\n"
                        + (f"Extra criterion: {criterion_override}\n" if criterion_override else "")
                        + f"\nURL: {url}\n\n"
                        "--- learned classification for this URL shape (PRIOR) ---\n"
                        + json.dumps(prior, ensure_ascii=False)
                        + "\n\nClassify this URL FROM the learned prior above (its `types` are the "
                        "expected source types); judge `relevance` against the Task and cite the "
                        "prior in `reason`."
                    )
                else:
                    content = _prepare_markdown(markdown)
                    hint = task_hint or "(no task hint provided — apply structural rules only)"
                    user_msg = (
                        f"Task: {hint}\n"
                        + (f"Extra criterion: {criterion_override}\n" if criterion_override else "")
                        + f"\nURL: {url}\n\n--- markdown ---\n{content}"
                    )

                result, _usage = await llm_service.complete_structured(
                    messages=[
                        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    response_model=NodeClassification,
                    profile="classify_urls",
                    model_tier=model_tier,
                    max_tokens=512,
                )
                rec = {"file_path": path, "url": url, **result.model_dump()}
                if hit_ref:
                    rec["skill_ref"] = hit_ref
                    rec["from_skill"] = True
                return rec
            except Exception as e:
                logger.warning("classify_urls helper failed for %s: %s", path[:80], e)
                # Safe defaults: bias toward 'relevant' + confidence=0 so the
                # main LLM knows it must verify this one itself.
                return {
                    "file_path": path,
                    "url": "",
                    "types": ["embedded"],
                    "relevance": "relevant",
                    "confidence": 0.0,
                    "reason": f"[helper_error] {type(e).__name__}: {str(e)[:160]}",
                    "evidence_excerpt": "",
                }

    classifications = await asyncio.gather(
        *[_classify_one(p) for p in node_paths]
    )

    # Quick stats for the main LLM to see at a glance
    n_total = len(classifications)
    n_failed = sum(1 for c in classifications if c["confidence"] == 0.0)
    n_not_relevant = sum(1 for c in classifications if c["relevance"] == "not_relevant")
    by_class: dict[str, int] = defaultdict(int)
    for c in classifications:
        for t in (c.get("types") or []):
            by_class[t] += 1

    logger.info(
        "agentic.classify_urls n=%d tier=%s failed=%d not_relevant=%d by_class=%s",
        n_total, model_tier, n_failed, n_not_relevant, dict(by_class),
    )
    n_skill_hits = sum(1 for c in classifications if c.get("from_skill"))
    signals: list[str] = []
    if n_failed > 0:
        signals.append(f"helper_errors:{n_failed}")
    if n_not_relevant > n_total * 0.5:
        signals.append("majority_not_relevant")
    if n_skill_hits > 0:
        signals.append(f"skill_hits:{n_skill_hits}")
    _log_tool_result("classify_urls", {
        "n_total": n_total,
        "n_failed": n_failed,
        "n_not_relevant": n_not_relevant,
        "n_skill_hits": n_skill_hits,
        "by_class": dict(by_class),
        "model_tier": model_tier,
    }, signals=signals or None)
    return _ok({
        "n_total": n_total,
        "n_failed": n_failed,
        "n_not_relevant": n_not_relevant,
        "n_skill_hits": n_skill_hits,
        "by_class": dict(by_class),
        "classifications": classifications,
    })


# ──────────────────────────────────────────────────────────────────────
# Batch concurrency wrappers — fan-out N calls in parallel
#
# The agent emits one batch tool_use block (single LLM turn) and the
# tool handler internally asyncio.gathers N underlying calls. This
# bypasses the model's tendency toward single-tool-per-turn and exposes
# the underlying IO concurrency to the agent. Use when the agent has
# many independent URLs/queries to process; otherwise the singular
# tools work fine.
#
# Concurrency caps are bounded by the underlying handler's own limits:
#   batch_probe_urls  → HEAD requests are cheap, allow up to 50
# Note: search_web and fetch_page are themselves parallel tools (accept
# queries=[...] / urls=[...] respectively); the legacy batch_search_web
# and batch_fetch_pages wrappers were removed since they became redundant.
# ──────────────────────────────────────────────────────────────────────


@tool(
    "batch_probe_urls",
    "HEAD-probe N URLs in parallel (up to 50 per call). Returns "
    "{n_total, n_dead, results} where each item is one probe_url-shape "
    "record: {url, is_alive, status_code, content_type, content_length, "
    "response_time_ms}.",
    {"urls": list, "timeout": float},
)
async def batch_probe_urls(args: dict[str, Any]) -> dict[str, Any]:
    _log_tool_call("batch_probe_urls", args)
    gate = _check_task_description()
    if gate is not None:
        return gate
    urls_raw = args.get("urls") or []
    if not isinstance(urls_raw, list) or not urls_raw:
        return _err("urls must be a non-empty list of strings")
    urls = [str(u).strip() for u in urls_raw if str(u).strip()][:50]
    if not urls:
        return _err("urls contained no valid entries")
    timeout = args.get("timeout")

    async def _one(u: str) -> dict[str, Any]:
        try:
            payload_args = {"url": u}
            if timeout is not None:
                payload_args["timeout"] = timeout
            env = await probe_url.handler(payload_args)
            return json.loads(env["content"][0]["text"])
        except Exception as e:
            logger.warning("batch_probe_urls sub-call failed for %s: %s", u[:60], e)
            return {"url": u, "is_alive": False, "error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*[_one(u) for u in urls])
    n_total = len(results)
    n_dead = sum(1 for r in results if not r.get("is_alive"))
    logger.info("agentic.batch_probe_urls n=%d dead=%d", n_total, n_dead)
    _log_tool_result("batch_probe_urls", {
        "n_total": n_total, "n_dead": n_dead,
    }, signals=[f"dead_urls:{n_dead}"] if n_dead else None)
    return _ok({"n_total": n_total, "n_dead": n_dead, "results": results})


# ──────────────────────────────────────────────────────────────────────
# Tool registry — what to expose to the agent
# ──────────────────────────────────────────────────────────────────────


ALL_TOOLS = [
    search_web,
    query_registry,
    fetch_page,
    firecrawl_map,
    crawl_list_tree,
    probe_url,
    batch_probe_urls,
    canonicalize_url,
    cluster_urls_by_skeleton,
    sample_cluster,
    # Talk to the user (persisted left-aligned chat bubble in the conversation)
    send_user_message,
    lookup_skill,
    propose_skill,
    flush_skills,
    # Self-correction surface for skills
    update_skill,
    delete_skill,
    consolidate_skills,
    # Cross-run prose memory (narrative/strategy/why — counterpart to skills)
    memory_append,
    memory_read,
    # Tool change proposal → offline ReviewAgent (added 2026-05-21,
    # collapsed 3 → 1 on 2026-05-22 for tighter MCP surface)
    propose_tool_change,
    # Hybrid classification helper (optional; main LLM remains decision maker)
    classify_urls,
    # Emit-as-you-go commit tools (decisions are persisted incrementally
    # to JSONL, avoiding end-of-context narrative drift)
    check_url_committed_status,
    commit_source,
    commit_portal_tree,
    remove_committed_source,
]
