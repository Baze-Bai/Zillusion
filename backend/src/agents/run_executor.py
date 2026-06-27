"""Detached discovery run body.

Extracted from the old ``POST /discover`` ``event_generator`` so the run can
execute as a background task (see ``services.run_registry``) decoupled from the
HTTP connection. Every SSE ``yield sse_event(...)`` became
``await handle.publish(...)``, which persists the event to the timeline FIRST
then fans it out to all live subscribers — so closing the tab no longer stops
the run, and review replays the same events.

This module also owns the run-environment / config snapshot helpers and the
process-level concurrency gate (moved here from the route so the route stays
thin and there's no route↔executor import cycle), plus three conversational
additions over the legacy stream:
  - ``session_titled``     — title refresh after parse_intent,
  - description sync       — sessions.description ← task_description.md,
  - ``source_committed`` / ``agent_text`` — forwarded from the discovery node.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import traceback
import uuid
from typing import TYPE_CHECKING, Any

from src.agents.graph import get_graph
from src.config import settings
from src.services.event_store import (
    load_session,
    max_seq,
    update_session_meta,
    update_session_status,
    upsert_artifact,
)
from src.services.run_logger import (
    RunLogger,
    log_event,
    reset_run_logger,
    set_run_logger,
)
from src.services.run_registry import RunHandle, reset_steer_queue, run_registry, set_steer_queue
from src.utils.request_context import reset_query_id, set_query_id

if TYPE_CHECKING:  # avoid a runtime import cycle with the route module
    from src.api.routes.discover import DiscoverRequest

logger = logging.getLogger(__name__)

_PIPELINE_NODES = {
    "parse_intent",
    "agentic_discovery",
    "judge",
    "reflect",
    "diagnostics_writeback",
    "finalize",
}


# ──────────────────────────────────────────────────────────────────────
# Run-environment snapshot (cached per process) — moved from the route.
# ──────────────────────────────────────────────────────────────────────
_RUN_ENVIRONMENT_CACHE: dict[str, Any] | None = None


def _git_state() -> dict[str, Any]:
    """Best-effort git SHA / branch / dirty status. All-None on failure."""
    info: dict[str, Any] = {"sha": None, "branch": None, "dirty": None}
    try:
        info["sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
    except Exception:
        pass
    try:
        info["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        info["dirty"] = bool(out.strip())
    except Exception:
        pass
    return info


def _system_prompt_fingerprint() -> dict[str, Any]:
    """SHA + char-count of the agent system prompt — detects prompt drift."""
    try:
        from src.agents.agentic.system_prompt import SYSTEM_PROMPT

        b = SYSTEM_PROMPT.encode("utf-8")
        return {"sha256": hashlib.sha256(b).hexdigest(), "chars": len(SYSTEM_PROMPT)}
    except Exception:
        return {"sha256": None, "chars": None}


def _config_snapshot() -> dict[str, Any]:
    """Snapshot of the config fields that meaningfully shape pipeline behavior."""
    s = settings
    return {
        "scoring": {
            "enable_stage_a_scoring": s.scoring.enable_stage_a_scoring,
            "enable_stage_a_tree_judge": s.scoring.enable_stage_a_tree_judge,
            "enable_stage_b_llm_ranker": s.scoring.enable_stage_b_llm_ranker,
            "enable_stage_b_llm_filtering": getattr(s.scoring, "enable_stage_b_llm_filtering", None),
            "stage_b_llm_filter_max_drop_ratio": getattr(s.scoring, "stage_b_llm_filter_max_drop_ratio", None),
            "enable_wrapper_url_blacklist": getattr(s.scoring, "enable_wrapper_url_blacklist", None),
            "enable_freshness_dim": s.scoring.enable_freshness_dim,
            "enable_geographic_fit": s.scoring.enable_geographic_fit,
            "enable_schema_coverage_fit": s.scoring.enable_schema_coverage_fit,
            "veto_relevance_threshold": s.scoring.veto_relevance_threshold,
            "veto_license_zero": s.scoring.veto_license_zero,
            "retired_overall_cap": s.scoring.retired_overall_cap,
        },
        "budget": {
            "stage_a_batch_size": s.budget.stage_a_batch_size,
            "relevance_threshold_stage_a": s.budget.relevance_threshold_stage_a,
            "max_candidates_stage_b": s.budget.max_candidates_stage_b,
            "max_iterations": s.budget.max_iterations,
            "max_portals_to_map": s.budget.max_portals_to_map,
            "max_scrape_per_portal": s.budget.max_scrape_per_portal,
            "max_total_scrape": s.budget.max_total_scrape,
            "max_concurrent_discoveries": s.budget.max_concurrent_discoveries,
        },
        "judging": {
            "enable_stage_a_probe": s.judging.enable_stage_a_probe,
            "stage_a_probe_timeout": s.judging.stage_a_probe_timeout,
        },
        "llm": {
            "primary_strong": s.llm.primary_strong,
            "primary_fast": s.llm.primary_fast,
            "primary_reasoning": s.llm.primary_reasoning,
            "fallback_strong": s.llm.fallback_strong,
            "fallback_fast": s.llm.fallback_fast,
            "china_strong": s.llm.china_strong,
            "china_fast": s.llm.china_fast,
            "temperature": s.llm.temperature,
            "request_timeout": s.llm.request_timeout,
            "deepseek_thinking_enabled": getattr(s.llm, "deepseek_thinking_enabled", None),
            "zai_thinking_enabled": getattr(s.llm, "zai_thinking_enabled", None),
        },
        "diagnostics": {
            "enabled": s.diagnostics.enabled,
            "slow_call_ms": s.diagnostics.slow_call_ms,
        },
    }


def _build_run_environment() -> dict[str, Any]:
    """Assemble (and cache) the per-process run-environment snapshot."""
    global _RUN_ENVIRONMENT_CACHE
    if _RUN_ENVIRONMENT_CACHE is not None:
        return _RUN_ENVIRONMENT_CACHE
    try:
        from src.main import SERVICE_VERSION
    except Exception:
        SERVICE_VERSION = "unknown"
    snapshot = {
        "service_version": SERVICE_VERSION,
        "git": _git_state(),
        "system_prompt": _system_prompt_fingerprint(),
        "config": _config_snapshot(),
    }
    _RUN_ENVIRONMENT_CACHE = snapshot
    return snapshot


# ──────────────────────────────────────────────────────────────────────
# Process-level /discover gate — bounds concurrent RUNS (not subscribers).
# ──────────────────────────────────────────────────────────────────────
_discover_semaphore: asyncio.Semaphore | None = None


def _get_discover_semaphore() -> asyncio.Semaphore:
    global _discover_semaphore
    if _discover_semaphore is None:
        _discover_semaphore = asyncio.Semaphore(settings.budget.max_concurrent_discoveries)
    return _discover_semaphore


def _build_initial_state(request: "DiscoverRequest", request_id: str, query_id: str) -> dict[str, Any]:
    return {
        "query": request.query,
        "user_id": "anonymous",
        "request_id": request_id,
        "query_id": query_id,
        "iteration": 0,
        "max_iterations": request.max_iterations,
        "cost_accumulated": 0.0,
        "stage_timings": {},
        "errors": [],
        "portal_budget": {
            "max_portals_to_map": settings.budget.max_portals_to_map,
            "max_scrape_per_portal": settings.budget.max_scrape_per_portal,
            "max_total_scrape": settings.budget.max_total_scrape,
            "portals_mapped": 0,
            "total_scraped": 0,
        },
    }


async def execute_discovery_run(
    handle: RunHandle,
    request: "DiscoverRequest",
    request_id: str,
    query_id: str,
    *,
    rerun: bool = False,
) -> None:
    """Run the discovery graph to completion, publishing every event through
    ``handle``. Owns its own terminal state (mark_done on success/error); the
    RunRegistry supervisor is only a last-resort safety net.

    ``rerun=True`` marks a feedback-restart (restart_discovery_with_feedback):
    its request.query is the SYSTEM-COMPOSED enriched prompt (original query +
    follow-up marker + the user's note), and the flag rides query_accepted so
    the frontend does NOT render that composite as a fresh user bubble — the
    user's note is already on screen as its own steer bubble."""
    # Bind ContextVars INSIDE the task — PEP 567 copies context at task-creation
    # time, so these must be set here (not in the route) to scope to this run.
    qid_token = set_query_id(query_id)
    rl_token = set_run_logger(RunLogger(query_id=query_id, query_text=request.query))
    # Expose the run's steering queue to the discovery node (turn-level steering).
    steer_token = set_steer_queue(handle.input_queue)
    loop = asyncio.get_event_loop()
    run_start_t = loop.time()

    nodes_seen: dict[str, dict] = {}
    latest_cost_accumulated = 0.0
    latest_stage_timings: dict[str, float] = {}
    completed = False
    n_sources_final: int | None = None

    initial_state = _build_initial_state(request, request_id, query_id)

    log_event("run_environment", _build_run_environment())
    log_event("run_config", {
        "request_id": request_id,
        "query": request.query,
        "max_iterations": request.max_iterations,
        "license_constraint": request.license_constraint,
        "budget_constraint": request.budget_constraint,
        "geographic_scope": request.geographic_scope,
        "temporal_range": request.temporal_range,
        "desired_formats": request.desired_formats,
        "portal_budget": initial_state.get("portal_budget"),
        "experiment_tag": request.experiment_tag,
        "baseline_run_ref": request.baseline_run_ref,
        "expected_outcome": request.expected_outcome,
    })

    graph = get_graph()
    sem = _get_discover_semaphore()
    async with sem:
        try:
            await handle.publish("query_accepted", {
                "query_id": query_id,
                "request_id": request_id,
                "query": request.query,
                "rerun": rerun,
            })

            config = {"configurable": {"thread_id": query_id}}

            async for event in graph.astream_events(initial_state, config=config, version="v2"):
                event_kind = event.get("event", "")
                event_name = event.get("name", "")

                # ── Mid-run custom events from inside agentic_discovery ──
                if event_kind == "on_custom_event" and event_name == "task_description_committed":
                    payload = event.get("data") or {}
                    content = payload.get("content")
                    await handle.publish("task_description_committed", {
                        "query_id": query_id,
                        "file_path": payload.get("file_path"),
                        "chars": payload.get("chars"),
                        "content": content,
                    })
                    # Mirror the agent's task_description.md Goal into the
                    # session's persisted description (sidebar / detail view).
                    if content:
                        try:
                            await update_session_meta(query_id, description=content)
                        except Exception:
                            logger.debug("update description failed", exc_info=True)
                    continue

                if event_kind == "on_custom_event" and event_name == "agent_text":
                    await handle.publish("agent_text", event.get("data") or {})
                    continue

                if event_kind == "on_custom_event" and event_name == "agent_message":
                    # The agent's deliberate message to the user → persisted +
                    # fanned out as a left-aligned chat bubble.
                    await handle.publish("agent_message", event.get("data") or {})
                    continue

                if event_kind == "on_custom_event" and event_name == "source_committed":
                    await handle.publish("source_committed", event.get("data") or {})
                    continue

                # ── Node start ──
                if event_kind == "on_chain_start" and event_name in _PIPELINE_NODES:
                    nodes_seen[event_name] = {"start": True}
                    log_event("node_start", {"node": event_name})
                    await handle.publish("stage_start", {"stage": event_name})

                # ── Node end ──
                elif event_kind == "on_chain_end" and event_name in _PIPELINE_NODES:
                    output = event.get("data", {}).get("output", {})
                    timings = output.get("stage_timings", {})
                    duration = timings.get(event_name, 0)
                    cost = output.get("cost_accumulated", 0)

                    nc_summary: dict[str, Any] = {
                        "duration_ms": round(duration * 1000, 2),
                        "cost_usd": cost,
                        "output_keys": list(output.keys()),
                    }
                    if event_name == "agentic_discovery":
                        srcs = output.get("deduplicated_sources") or []
                        trees = output.get("portal_trees") or []
                        nc_summary["n_sources"] = len(srcs)
                        nc_summary["n_portal_trees"] = len(trees)
                    elif event_name == "judge":
                        nc_summary["n_scored"] = len(output.get("scored_sources") or [])
                    elif event_name == "finalize":
                        rep = output.get("final_report")
                        if rep is not None:
                            try:
                                nc_summary["report_n_sources_total"] = int(getattr(rep, "total_found", 0) or 0)
                                nc_summary["report_n_ranked"] = len(getattr(rep, "all_sources_ranked", []) or [])
                                nc_summary["report_n_api"] = len(getattr(rep, "api_sources", []) or [])
                                nc_summary["report_n_file"] = len(getattr(rep, "file_sources", []) or [])
                                nc_summary["report_n_embedded"] = len(getattr(rep, "embedded_sources", []) or [])
                            except Exception:
                                pass
                    log_event("node_complete", {"node": event_name, **nc_summary})

                    latest_cost_accumulated = max(latest_cost_accumulated, float(cost or 0))
                    if isinstance(timings, dict):
                        for k, v in timings.items():
                            try:
                                latest_stage_timings[k] = float(v)
                            except (TypeError, ValueError):
                                pass
                    nodes_seen[event_name] = {
                        "duration_ms": round(duration * 1000, 2),
                        "cost_usd": cost,
                    }

                    await handle.publish("stage_complete", {
                        "stage": event_name,
                        "duration_ms": round(duration * 1000),
                        "cost_usd": cost,
                    })

                    if event_name == "parse_intent":
                        requirement = output.get("requirement")
                        if requirement is not None:
                            try:
                                payload = (
                                    requirement.model_dump(mode="json")
                                    if hasattr(requirement, "model_dump")
                                    else requirement
                                )
                            except Exception:
                                payload = str(requirement)
                            await handle.publish("parse_intent_result", payload)
                            # Refresh the sidebar title from the parsed intent.
                            title = (getattr(requirement, "title", "") or "").strip()
                            if title:
                                try:
                                    await update_session_meta(query_id, title=title)
                                except Exception:
                                    logger.debug("update title failed", exc_info=True)
                                await handle.publish("session_titled", {"query_id": query_id, "title": title})

                    elif event_name == "agentic_discovery":
                        sources = output.get("deduplicated_sources", [])
                        await handle.publish("progress", {
                            "stage": "agentic_discovery",
                            "candidates_found": len(sources),
                        })

                    elif event_name == "judge":
                        scored = output.get("scored_sources", [])
                        if scored:
                            partial = [
                                {
                                    "id": s.id,
                                    "name": s.name,
                                    "url": s.url,
                                    "source_type": s.source_type.value,
                                    "description": s.description[:200],
                                    "scores": s.scores.model_dump() if s.scores else None,
                                }
                                for s in scored[:10]
                            ]
                            await handle.publish("partial_sources", partial)

                    elif event_name == "finalize":
                        report = output.get("final_report")
                        if report:
                            await handle.publish("done", {
                                "report": report.model_dump(mode="json"),
                                "query_id": query_id,
                            })
                            try:
                                n_sources_final = int(getattr(report, "total_found", 0) or 0)
                            except Exception:
                                n_sources_final = None
                            # Surface the final report as a viewable file (right
                            # panel). finalize wrote it to the session workspace.
                            try:
                                from pathlib import Path

                                fr = (
                                    Path("agent-workspace/agent-sessions") / query_id / "final_report.json"
                                ).resolve()
                                if fr.is_file():
                                    aid = await upsert_artifact(
                                        query_id,
                                        kind="final_report",
                                        path=str(fr),
                                        meta={"n_sources": n_sources_final},
                                    )
                                    await handle.publish("artifact_ready", {
                                        "artifact_id": aid,
                                        "kind": "final_report",
                                        "filename": "final_report.json",
                                        "content_type": "json",
                                    })
                            except Exception:
                                logger.debug("final_report artifact failed", exc_info=True)

            completed = True
            await handle.mark_done(
                "completed",
                total_cost_usd=round(latest_cost_accumulated, 6),
                n_sources=n_sources_final,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "discover.pipeline_error",
                extra={"event": "discover.pipeline_error", "error": str(e)},
            )
            log_event("error", {
                "scope": "pipeline",
                "error_type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            })
            try:
                await handle.publish("error", {
                    "message": str(e),
                    "query_id": query_id,
                    "request_id": request_id,
                })
            except Exception:
                pass
            await handle.mark_done("error", error=str(e))
        finally:
            if completed:
                logger.info(
                    "discover.completed",
                    extra={
                        "event": "discover.completed",
                        "nodes_executed": list(nodes_seen.keys()),
                        "node_timings": nodes_seen,
                    },
                )
            total_ms = round((loop.time() - run_start_t) * 1000, 2)
            log_event("run_complete", {
                "completed": completed,
                "nodes_executed": list(nodes_seen.keys()),
                "total_duration_ms": total_ms,
                "cumulative_cost_usd": round(latest_cost_accumulated, 6),
                "stage_timings_seconds": {k: round(v, 3) for k, v in latest_stage_timings.items()},
            })
            reset_run_logger(rl_token)
            reset_query_id(qid_token)
            reset_steer_queue(steer_token)


async def restart_discovery_run(query_id: str, feedback: str) -> str | None:
    """Re-run discovery for a TERMINAL/idle session, carrying user feedback.

    Starts a fresh run on the SAME session: query = original query + the feedback
    (so parse_intent re-reads intent with it in mind and the whole pipeline
    re-runs), reusing the original run params from ``run_config``; events continue
    the session timeline (``start_seq = max_seq``). Returns the query_id if a run
    was (re)started, or None if a live run already exists or the session can't be
    resolved. Mirror of the harness-side ``restart_child_run``."""
    existing = run_registry.get(query_id)
    if existing is not None and not existing.is_terminal:
        return None  # live → caller should live-steer, not restart
    sess = await load_session(query_id)
    if sess is None:
        return None
    original_query = (sess.get("query_text") or "").strip()
    if not original_query:
        return None
    rc = sess.get("run_config") or {}
    enriched = (
        original_query
        + "\n\n[Follow-up feedback after the previous run — re-run discovery "
        + "taking this into account]:\n"
        + feedback.strip()
    )

    # Lazy import to avoid a module-load cycle (discover imports execute_discovery_run).
    from src.api.routes.discover import DiscoverRequest

    # model_construct bypasses validation (the enriched query may exceed the raw
    # query length limit / contain newlines; it's system-built, not user input).
    request = DiscoverRequest.model_construct(
        query=enriched,
        license_constraint=rc.get("license_constraint", "any"),
        budget_constraint=rc.get("budget_constraint", "any"),
        geographic_scope=rc.get("geographic_scope"),
        temporal_range=rc.get("temporal_range"),
        desired_formats=rc.get("desired_formats"),
        max_iterations=int(rc.get("max_iterations") or settings.budget.max_iterations),
        experiment_tag=rc.get("experiment_tag"),
        baseline_run_ref=rc.get("baseline_run_ref"),
        expected_outcome=rc.get("expected_outcome"),
    )
    request_id = uuid.uuid4().hex
    try:
        await update_session_status(query_id, "running")
    except Exception:  # noqa: BLE001
        logger.debug("restart_discovery_run: status flip failed", exc_info=True)
    await run_registry.start(
        query_id,
        lambda h: execute_discovery_run(h, request, request_id, query_id, rerun=True),
        start_seq=await max_seq(query_id),
    )
    return query_id
