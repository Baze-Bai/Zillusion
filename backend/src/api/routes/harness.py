"""Harness phase endpoints.

- POST /harness/{query_id}/start — run the downstream scraper-builder on the
  session's crawlable sources; events continue the SAME session timeline.
- POST /harness/{site_id}/steer — append operator guidance to a site's
  user_steering.md inbox; the /explore agent reads it at its next iteration.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as FPath
from pydantic import BaseModel, Field

from src.services.event_store import (
    create_session,
    load_session,
    max_seq,
    update_session_status,
)
from src.services.harness_orchestrator import (
    _WORKFLOW_FOR,
    harness_child_id,
    last_loop_verdict,
    restart_child_run,
    run_harness_for_source,
    run_production_for_source,
    site_inputs,
    site_workspace,
)
from src.services.run_registry import run_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["harness"])

# Allowed shape for site_id / query_id path params (anti path-traversal). Mirrors
# _SITE_ID_RE in harness_orchestrator; a non-matching value gets a clean 422
# before any handler — see also the containment assert in site_workspace.
SITE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"


class HarnessStartRequest(BaseModel):
    site_ids: list[str] | None = None
    # None = per-workflow-type default (extraction/download 5; api 2). An
    # explicit value wins for every type.
    max_iters: int | None = Field(default=None, ge=1, le=10)
    max_cost_usd: float = Field(default=20.0, gt=0, le=100)
    model: str | None = None


@router.post("/harness/{query_id}/start")
async def harness_start(
    query_id: Annotated[str, FPath(pattern=SITE_ID_PATTERN)], body: HarnessStartRequest
):
    """Build scrapers for a discovery session's sources. Each requested source
    runs as its OWN child session (nested under this discovery session) — its own
    page + sidebar entry — and children build in PARALLEL (one run per child)."""
    parent = await load_session(query_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Resolve the requested sources from the discovery final report.
    report_path = (
        Path("agent-workspace/agent-sessions") / query_id / "final_report.json"
    ).resolve()
    if not report_path.is_file():
        raise HTTPException(status_code=400, detail="no final_report for this session")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"final_report unreadable: {e}") from e

    # Read harness-bound sources from the GROUPED report lists (embedded/file/
    # api), not all_sources_ranked — the grouped lists carry `usage_guide`
    # (attached by finalize), so the scraper child session's source card shows
    # the guide too. (Same set; all_sources_ranked just lacks the guide field.)
    grouped = [
        *(report.get("embedded_sources") or []),
        *(report.get("file_sources") or []),
        *(report.get("api_sources") or []),
    ]
    crawlable = [s for s in grouped if s.get("source_type") in _WORKFLOW_FOR]
    if body.site_ids is not None:
        want = set(body.site_ids)
        crawlable = [s for s in crawlable if s.get("id") in want]
    if not crawlable:
        raise HTTPException(status_code=400, detail="no crawlable sources to build")

    created: list[str] = []
    for src in crawlable:
        wf = _WORKFLOW_FOR[src["source_type"]]
        child_qid = harness_child_id(src)

        # Already building? Parallel-safe — each child is its own run.
        existing = run_registry.get(child_qid)
        if existing is not None and not existing.is_terminal:
            created.append(child_qid)
            continue

        # Create the child session row once; on re-build just flip it running.
        if await load_session(child_qid) is None:
            await create_session(
                child_qid,
                query_text=src.get("url") or src.get("name") or child_qid,
                title=f"Scraper: {src.get('name') or child_qid}"[:120],
                parent_query_id=query_id,
                status="running",
                run_config={
                    "site_id": child_qid,
                    "source_id": src.get("id"),
                    "workflow_type": wf,
                    "url": src.get("url"),
                    "name": src.get("name"),
                },
            )
        else:
            await update_session_status(child_qid, "running")

        await run_registry.start(
            child_qid,
            lambda h, s=src, w=wf: run_harness_for_source(
                h, query_id, s, w,
                max_iters=body.max_iters, max_cost_usd=body.max_cost_usd, model=body.model,
            ),
            start_seq=await max_seq(child_qid),
        )
        created.append(child_qid)

    logger.info(
        "harness.started",
        extra={"event": "harness.started", "query_id": query_id, "children": created},
    )
    return {"accepted": True, "child_query_ids": created}


class HarnessSteerRequest(BaseModel):
    query_id: str
    content: str = Field(min_length=1, max_length=4000)


@router.post("/harness/{site_id}/steer")
async def harness_steer(
    site_id: Annotated[str, FPath(pattern=SITE_ID_PATTERN)], body: HarnessSteerRequest
):
    """Operator feedback for a harness site.

    The content is ALWAYS appended to the site's ``user_steering.md`` inbox (so it
    is carried into the next iter's SessionStart, concatenated with the prior
    iter's task_plan / iter_summary / workflow diff). Then we dispatch by run
    state:

      - run **LIVE** → delivered live (turn-level): the harness tails
        ``user_steering.md`` (which we just appended to) and feeds new blocks to
        the running ``/explore`` agent within ~1s. Still filed, so it also
        carries into the next iter / a later restart.
      - run **IDLE / terminal** → AUTO-RESTART the explore→validate loop now,
        carrying the just-filed feedback + the preserved workspace artifacts
        (default budget, matching Build-scraper: 5 iters / $20)."""
    ws = site_workspace(site_id)
    if not ws.is_dir():
        raise HTTPException(status_code=404, detail="site workspace not found")

    inbox = ws / "user_steering.md"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    block = f"\n## Operator steering @ {ts}\n\n{body.content.strip()}\n"
    try:
        with inbox.open("a", encoding="utf-8") as f:  # append, so multiple steers queue
            f.write(block)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"write failed: {e}") from e

    # site_id IS the child run's registry key (child query_id == bridge site_id).
    handle = run_registry.get(site_id)
    if handle is not None and not handle.is_terminal:
        if getattr(handle, "phase", "explore") == "run":
            # The live process is a production RUN — /explore is NOT running, so
            # its steering tail can't deliver this now. It stays filed (echoed
            # for visibility) and is injected at the next explore loop's start.
            await handle.publish(
                "harness_user_steer",
                {"site_id": site_id, "content": body.content, "disposition": "queued_next_explore"},
            )
            return {
                "accepted": True, "applies": "next_run", "restarted": False,
                "note": "a production run is live; guidance saved for the next explore loop",
            }
        # Live explore loop: the harness tails user_steering.md (just appended
        # above) and delivers this guidance to the running /explore agent at its
        # next turn (~1s poll). It's filed too, so it also carries into the next
        # iter.
        await handle.publish(
            "harness_user_steer",
            {"site_id": site_id, "content": body.content, "disposition": "delivered_live"},
        )
        return {"accepted": True, "applies": "live", "restarted": False}

    # Idle / terminal run: auto-restart now, carrying the feedback we just filed
    # (max_iters None = per-type default: extraction/download 5, api 2).
    child_qid = await restart_child_run(site_id, max_cost_usd=20.0)
    if child_qid is None:
        # No live run and the source couldn't be resolved to restart (e.g. session
        # metadata absent) — the feedback stays filed for a manual re-build.
        return {
            "accepted": True, "applies": "next_run", "restarted": False,
            "note": "no live run and could not auto-restart; feedback saved for next build",
        }
    new_handle = run_registry.get(child_qid)
    if new_handle is not None:
        await new_handle.publish(
            "harness_user_steer",
            {"site_id": site_id, "content": body.content, "disposition": "restarted"},
        )
    return {
        "accepted": True, "applies": "restarted", "restarted": True,
        "child_query_id": child_qid,
    }


class HarnessCredentialsRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)
    extra: dict[str, str] = Field(default_factory=dict)


@router.post("/harness/{site_id}/credentials")
async def harness_credentials(
    site_id: Annotated[str, FPath(pattern=SITE_ID_PATTERN)], body: HarnessCredentialsRequest
):
    """Supply the user-obtained API key for a key-gated api site and launch its
    explore→validate loop.

    Writes ``inputs/<site>/credentials.json`` (the file the explore agent, the
    validator's isolated rerun, and the production runner all consume), clears
    the bridge's awaiting marker, then relaunches the build exactly like a
    steer-restart. The key value is never logged and never echoed back."""
    inp = site_inputs(site_id)
    if not (inp / "api_spec.json").is_file():
        raise HTTPException(status_code=404, detail="not an api-staged site (no api_spec.json)")

    payload = {"api_key": body.api_key, "extra": body.extra}
    try:
        inp.mkdir(parents=True, exist_ok=True)
        (inp / "credentials.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"credentials write failed: {e}") from e
    # The durable awaiting marker is obsolete the moment the key exists.
    with contextlib.suppress(OSError):
        (inp / "credentials_needed.json").unlink(missing_ok=True)

    logger.info(
        "harness.credentials_received",
        extra={"event": "harness.credentials_received", "site_id": site_id},
    )

    # A live run already holds this site (e.g. an open-API explore in flight) —
    # the key is saved for the next isolated rerun / production run; don't
    # collide with the loop.
    existing = run_registry.get(site_id)
    if existing is not None and not existing.is_terminal:
        return {"accepted": True, "launched": False, "note": "a run is live; key saved for it"}

    child_qid = await restart_child_run(site_id)
    if child_qid is None:
        # Key saved, but the child's session metadata can't resolve the source
        # (e.g. a bridge-CLI-staged site with no web session) — launch manually.
        return {
            "accepted": True, "launched": False,
            "note": "key saved; could not auto-launch (no resolvable child session)",
        }
    new_handle = run_registry.get(child_qid)
    if new_handle is not None:
        await new_handle.publish("harness_credentials_received", {"site_id": site_id})
    return {"accepted": True, "launched": True, "child_query_id": child_qid}


class HarnessRunRequest(BaseModel):
    crawl_mode: str = Field(default="full", pattern="^(full|sample)$")
    wall_clock_cap_s: float = Field(default=3600.0, gt=0, le=86400)
    stall_timeout_s: float = Field(default=300.0, gt=0, le=7200)
    model: str | None = None
    # User-owned override: run despite an INCONCLUSIVE loop verdict ("仍要运行,
    # 自担风险"). Only unlocks INCONCLUSIVE — FAIL / no-verdict stay locked.
    force: bool = False


@router.post("/harness/{site_id}/run")
async def harness_run(
    site_id: Annotated[str, FPath(pattern=SITE_ID_PATTERN)], body: HarnessRunRequest
):
    """Start the Run (production crawl) phase for a scraper child whose
    explore→validate loop PASSED — runs the VALIDATED workflow.py at FULL scope,
    relaying ``run_*`` events into the SAME child session timeline (so the run
    continues on the same conversation page, below explore/validate).

    Gated to mirror the frontend button: there must be a PASSed loop — or an
    INCONCLUSIVE one with the user's explicit ``force`` override — no live run
    (explore/validate or a prior run), and a produced workflow.py. The run is a
    NEW RunRegistry run keyed by the same site_id, seeded at the current max seq
    so its events continue the timeline."""
    ws = site_workspace(site_id)
    if not ws.is_dir():
        raise HTTPException(status_code=404, detail="site workspace not found")
    if not (ws / "workflow.py").is_file():
        raise HTTPException(status_code=400, detail="no workflow.py (run explore→validate first)")

    existing = run_registry.get(site_id)
    if existing is not None and not existing.is_terminal:
        raise HTTPException(status_code=409, detail="a run is already in progress for this site")

    verdict = await last_loop_verdict(site_id)
    if verdict != "PASS" and not (body.force and verdict == "INCONCLUSIVE"):
        raise HTTPException(status_code=409, detail="explore→validate has not passed for this site")

    await update_session_status(site_id, "running")
    handle = await run_registry.start(
        site_id,
        lambda h: run_production_for_source(
            h, site_id,
            crawl_mode=body.crawl_mode,
            wall_clock_cap_s=body.wall_clock_cap_s,
            stall_timeout_s=body.stall_timeout_s,
            model=body.model,
        ),
        start_seq=await max_seq(site_id),
    )
    # Stamp the phase NOW (the body also sets it, but only once scheduled) so a
    # steer racing in right after this POST classifies the live handle correctly.
    handle.phase = "run"  # type: ignore[attr-defined]
    logger.info(
        "harness.run_started",
        extra={"event": "harness.run_started", "site_id": site_id},
    )
    return {"accepted": True, "site_id": site_id}


class RunSteerRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


@router.post("/harness/{site_id}/run/steer")
async def harness_run_steer(
    site_id: Annotated[str, FPath(pattern=SITE_ID_PATTERN)], body: RunSteerRequest
):
    """Guidance for the Run (production crawl) agent — the chat box on the RUN
    page talking to the Run agent.

    The content is appended to the site's ``user_run_steering.md`` inbox. Then,
    by run state:

      - run **LIVE** → the run agent tails the inbox (~1s) and injects the
        message into its live SDK session via streaming-input.
      - run **IDLE / terminal** (and the loop verdict is PASS) → AUTO-RERUN the
        validated workflow now, carrying the just-filed message into the new
        session via ``steer_resume_offset`` (the run agent's tail starts at the
        pre-append size instead of EOF) — mirrors the explore steer's
        auto-restart semantics.
      - a live EXPLORE loop holds the registry key → can't deliver to a run nor
        start one; the note stays filed for the next run."""
    ws = site_workspace(site_id)
    if not ws.is_dir():
        raise HTTPException(status_code=404, detail="site workspace not found")

    inbox = ws / "user_run_steering.md"
    # Pre-append size: an auto-rerun's agent must tail from HERE so the message
    # we are about to file is delivered (its default offset is EOF at boot).
    try:
        pre_offset = inbox.stat().st_size if inbox.is_file() else 0
    except OSError:
        pre_offset = 0
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    block = f"\n## Operator steering @ {ts}\n\n{body.content.strip()}\n"
    try:
        with inbox.open("a", encoding="utf-8") as f:  # append, so steers queue
            f.write(block)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"write failed: {e}") from e

    # site_id IS the run's registry key (child query_id == bridge site_id).
    handle = run_registry.get(site_id)
    if handle is not None and not handle.is_terminal:
        if getattr(handle, "phase", "explore") == "run":
            # Live run: delivered within ~1s via the agent's steering tail.
            await handle.publish("run_user_steer", {"site_id": site_id, "content": body.content})
            return {"accepted": True, "delivered": True, "applies": "live"}
        # A live EXPLORE loop occupies this site — no run agent to deliver to,
        # and starting one now would collide with the loop. Keep it filed.
        return {
            "accepted": True, "delivered": False, "applies": "next_run",
            "note": "explore loop is live; message saved for the next run",
        }

    # Idle / terminal: chat on the run page RE-RUNS the validated workflow,
    # carrying this message into the new session. Gated like POST /run.
    if not (ws / "workflow.py").is_file() or await last_loop_verdict(site_id) != "PASS":
        return {
            "accepted": True, "delivered": False, "applies": "next_run",
            "note": "run idle and not re-runnable (no PASSed loop); message saved",
        }
    await update_session_status(site_id, "running")
    new_handle = await run_registry.start(
        site_id,
        lambda h: run_production_for_source(h, site_id, steer_resume_offset=pre_offset),
        start_seq=await max_seq(site_id),
    )
    new_handle.phase = "run"  # type: ignore[attr-defined]
    await new_handle.publish(
        "run_user_steer",
        {"site_id": site_id, "content": body.content, "disposition": "restarted"},
    )
    logger.info(
        "harness.run_restarted_by_steer",
        extra={"event": "harness.run_restarted_by_steer", "site_id": site_id},
    )
    return {"accepted": True, "delivered": True, "applies": "restarted", "restarted": True}
