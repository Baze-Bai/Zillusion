"""Stage 8.6: Diagnostics Writeback — per-query summary for external ReviewAgent.

Runs once per query, on the way out of the pipeline:

    reflect → (sufficient/max) → diagnostics_writeback → finalize

Purpose: produce one structured JSON record per /discover call that
captures the SHAPE of the run (iterations, unresolved gaps, tool-call
histogram, signal counts, domain coverage) for an offline ReviewAgent to
consume in batch. The ReviewAgent reads accumulated diagnostics across N
days and proposes CODE-level changes (new adapters, new tools, prompt
edits) as git PRs — none of which is read back into the runtime pipeline.

Scope: this node writes diagnostics about agent/prompt/tool behavior (a
code-flow signal for an offline reviewer). URL-classification learning is a
separate concern, handled by the skill library + its closed-loop correction
(see src.services.skill_library), not by this node.

Why we read the run-log JSONL instead of accumulating in memory:
  * The run-log is already the authoritative trace — duplicating its
    aggregates as in-memory state risks drift when one is updated but
    the other isn't.
  * Reading at end-of-query is one fopen + line iteration; cost is well
    under 100ms for typical run-log sizes (a few thousand events).
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.config import settings
from src.models.data_source import DataSource
from src.models.state import AgentState
from src.services.run_logger import get_run_logger

logger = logging.getLogger(__name__)


def _summarize_tool_proposals(query_id: str) -> dict[str, Any]:
    """Build the ``tool_proposals`` summary for the diagnostics blob.

    Looks at ``<TOOL_PROPOSALS_workspace_dir>/<today>/<query_id>.jsonl``
    (the file the propose_tool_change handler appends one line to per
    call, with ``kind`` ∈ {new, modify, remove}). Returns:

        {
            "enabled": bool,
            "count": int,                      # total proposals
            "by_kind": {"new": int, "modify": int, "remove": int},
            "by_priority": {"low": int, "medium": int, "high": int},
            "file": str | None,                # absolute path; None when absent
        }

    Never raises — a missing or unreadable file just yields count=0.
    """
    summary: dict[str, Any] = {
        "enabled": bool(settings.tool_proposals.enabled),
        "count": 0,
        "by_kind": {"new": 0, "modify": 0, "remove": 0},
        "by_priority": {"low": 0, "medium": 0, "high": 0},
        "file": None,
    }
    if not settings.tool_proposals.enabled:
        return summary

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = Path(settings.tool_proposals.workspace_dir) / today / f"{query_id}.jsonl"
    if not path.exists():
        return summary

    summary["file"] = str(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                summary["count"] += 1
                kind = rec.get("kind")
                if kind in summary["by_kind"]:
                    summary["by_kind"][kind] += 1
                pri = rec.get("priority")
                if pri in summary["by_priority"]:
                    summary["by_priority"][pri] += 1
    except OSError as exc:
        logger.warning("tool_proposals summary read failed (%s): %s", path, exc)

    return summary


def _registrable_etld1(host: str) -> str:
    """Registrable eTLD+1 from a host (``a.b.example.com`` → ``example.com``)."""
    if not host:
        return ""
    parts = host.lower().split(".")
    if len(parts) < 2:
        return host.lower()
    return ".".join(parts[-2:])


def _parse_run_log(path: Path) -> dict[str, Any]:
    """Walk the JSONL run-log and aggregate the fields we need.

    Returns:
      tool_histogram:   Counter of tool_name → call count
      signal_counts:    Counter of signal_tag → total occurrences across tools
      tool_signal_breakdown: dict of tool_name → Counter of signal_tag → count
                       (useful for "fetch_page got duplicate_fetch 8 times")
      tool_errors:      Counter of tool_name → error count
      reflect_iterations: int, observed reflect_decision events
      total_events:     int, sanity check
    """
    tool_histogram: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    tool_signal_breakdown: dict[str, Counter[str]] = {}
    tool_errors: Counter[str] = Counter()
    http_errors: Counter[str] = Counter()
    http_error_samples: list[dict[str, Any]] = []
    reflect_iterations = 0
    total_events = 0

    if not path.exists():
        return {
            "tool_histogram": {},
            "signal_counts": {},
            "tool_signal_breakdown": {},
            "tool_errors": {},
            "http_errors": {},
            "http_error_samples": [],
            "reflect_iterations": 0,
            "total_events": 0,
        }

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_events += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = rec.get("event")
                data = rec.get("data") or {}

                if event == "tool_call":
                    tool_name = data.get("tool") or "unknown"
                    tool_histogram[tool_name] += 1
                elif event == "tool_result":
                    tool_name = data.get("tool") or "unknown"
                    for sig in data.get("signals") or []:
                        signal_counts[sig] += 1
                        breakdown = tool_signal_breakdown.setdefault(tool_name, Counter())
                        breakdown[sig] += 1
                elif event == "tool_error":
                    tool_name = data.get("tool") or "unknown"
                    tool_errors[tool_name] += 1
                elif event == "http_error":
                    # Outbound HTTP failures (adapter registries, firecrawl,
                    # etc.) emitted by http_client / firecrawl_client. Keyed by
                    # provider so the ReviewAgent sees "ckan failed 7x" instead
                    # of a clean run. A few full samples are kept for triage.
                    provider = data.get("provider") or "unknown"
                    http_errors[provider] += 1
                    if len(http_error_samples) < 12:
                        http_error_samples.append({
                            "provider": provider,
                            "host": data.get("host"),
                            "status": data.get("status"),
                            "error": (str(data.get("error") or ""))[:200],
                        })
                elif event == "reflect_decision":
                    reflect_iterations += 1
    except OSError as exc:
        logger.warning("diagnostics_writeback: failed to read run-log %s: %s", path, exc)

    return {
        "tool_histogram": dict(tool_histogram),
        "signal_counts": dict(signal_counts),
        "tool_signal_breakdown": {
            k: dict(v) for k, v in tool_signal_breakdown.items()
        },
        "tool_errors": dict(tool_errors),
        "http_errors": dict(http_errors),
        "http_error_samples": http_error_samples,
        "reflect_iterations": reflect_iterations,
        "total_events": total_events,
    }


def _domains_touched(scored: list[DataSource]) -> list[dict[str, Any]]:
    """Compute per-domain summary from the final scored source list.

    Each entry: {etld1, n_sources, source_types, adapter_hint}.

    ``adapter_hint`` is best-effort — derived from metadata.get("adapter")
    or metadata.get("source") which our adapter-sourced candidates carry.
    None means we can't tell whether an adapter was used; cross-reference
    with `tool_signal_breakdown["firecrawl_map"]["zero_links"]` etc. for
    a fuller picture.
    """
    by_etld1: dict[str, dict[str, Any]] = {}
    for s in scored:
        try:
            etld1 = _registrable_etld1(urlparse(s.url).netloc)
        except Exception:
            continue
        if not etld1:
            continue
        bucket = by_etld1.setdefault(etld1, {
            "etld1": etld1,
            "n_sources": 0,
            "source_types": Counter(),
            "adapter_hint": None,
        })
        bucket["n_sources"] += 1
        bucket["source_types"][s.source_type.value] += 1
        # Surface adapter hint if any source from this domain advertised one
        if bucket["adapter_hint"] is None:
            meta = s.metadata or {}
            hint = meta.get("adapter") or meta.get("source")
            if hint:
                bucket["adapter_hint"] = str(hint)[:60]

    out = []
    for v in by_etld1.values():
        out.append({
            "etld1": v["etld1"],
            "n_sources": v["n_sources"],
            "source_types": dict(v["source_types"]),
            "adapter_hint": v["adapter_hint"],
        })
    # Largest contributors first
    out.sort(key=lambda d: -d["n_sources"])
    return out


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically via temp file + os.replace.

    Same pattern as SkillLibrary._write_atomic — same-dir tempfile then
    rename so partial writes never leave a half-written diagnostics file.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".tmp.diag.",
        suffix=".json",
        delete=False,
    ) as tf:
        tf.write(text)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_path = Path(tf.name)
    os.replace(tmp_path, path)


async def diagnostics_writeback_node(state: AgentState) -> dict:
    """Drop one diagnostics JSON capturing the shape of this query's run."""
    start = time.monotonic()

    if not settings.diagnostics.enabled:
        return {
            "stage_timings": {
                **state.get("stage_timings", {}),
                "diagnostics_writeback": 0,
            },
        }

    rl = get_run_logger()
    run_log_path = rl.path if rl else None

    # Identifiers
    query_id = state.get("query_id") or "anon"
    request_id = state.get("request_id") or ""
    requirement = state.get("requirement")
    user_query = getattr(requirement, "original_query", "") if requirement else state.get("query", "")
    domain = getattr(requirement, "domain", "") if requirement else ""

    # Iteration / critic state
    iterations = int(state.get("iteration", 0) or 0)
    critic = state.get("critic_feedback")
    is_sufficient = bool(getattr(critic, "is_sufficient", False)) if critic else False
    unresolved_gaps = list(getattr(critic, "gaps", []) or []) if (critic and not is_sufficient) else []
    next_round_feedback = getattr(critic, "next_round_feedback", "") if critic else ""

    # Scored sources → domain coverage
    scored: list[DataSource] = state.get("scored_sources") or []
    domains = _domains_touched(scored)

    # Run-log aggregates
    log_aggs: dict[str, Any] = (
        _parse_run_log(run_log_path) if run_log_path else {
            "tool_histogram": {}, "signal_counts": {},
            "tool_signal_breakdown": {}, "tool_errors": {},
            "http_errors": {}, "http_error_samples": [],
            "reflect_iterations": 0, "total_events": 0,
        }
    )

    # Cost / timing from existing accumulators
    cost_usd = float(state.get("cost_accumulated", 0.0) or 0.0)
    stage_timings = dict(state.get("stage_timings", {}) or {})

    # Tool change proposals — read the date-sharded JSONL the agent may
    # have appended to via propose_tool_change(kind=...). We cross-reference
    # rather than inline because proposals can be long (rationale + evidence
    # prose) and inflating every diagnostics blob would hurt ReviewAgent
    # batch reads.
    tool_proposals_summary = _summarize_tool_proposals(query_id)

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query_id": query_id,
        "request_id": request_id,
        "user_query": user_query,
        "domain": domain,

        "iterations": iterations,
        "final_sufficient": is_sufficient,
        "unresolved_gaps": unresolved_gaps,
        "next_round_feedback": (next_round_feedback or "")[:600],

        "n_scored_sources": len(scored),
        "domains_touched": domains,

        "tool_histogram": log_aggs["tool_histogram"],
        "tool_errors": log_aggs["tool_errors"],
        "http_errors": log_aggs["http_errors"],
        "http_error_samples": log_aggs["http_error_samples"],
        "signal_counts": log_aggs["signal_counts"],
        "tool_signal_breakdown": log_aggs["tool_signal_breakdown"],
        "reflect_iterations": log_aggs["reflect_iterations"],
        "total_log_events": log_aggs["total_events"],

        "cost_usd": cost_usd,
        "stage_timings": stage_timings,
        "run_log_path": str(run_log_path) if run_log_path else None,

        # Agent-emitted tool change proposals (optional channel).
        # ReviewAgent should open ``tool_proposals.file`` to read the
        # full JSONL when ``tool_proposals.count > 0``.
        "tool_proposals": tool_proposals_summary,
    }

    # File path: <workspace>/<YYYY-MM-DD>/<query_id>.json
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = Path(settings.diagnostics.workspace_dir) / today / f"{query_id}.json"
    try:
        _write_atomic(out_path, payload)
    except Exception as exc:
        logger.warning("diagnostics_writeback: write failed (%s): %s", out_path, exc)

    duration = time.monotonic() - start
    logger.info(
        "diagnostics_writeback complete: query_id=%s sources=%d tools=%d signals=%d "
        "tool_proposals=%d → %s (%.2fs)",
        query_id, len(scored), len(payload["tool_histogram"]),
        sum(payload["signal_counts"].values()),
        tool_proposals_summary["count"], out_path, duration,
    )

    return {
        "stage_timings": {
            **state.get("stage_timings", {}),
            "diagnostics_writeback": duration,
        },
    }
