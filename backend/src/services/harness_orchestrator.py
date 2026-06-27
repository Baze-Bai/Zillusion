"""Backend orchestration of the Claude Code Browser Harness.

After a discovery run produces a FinalReport, this spawns the harness
``explore-loop`` (one subprocess per crawlable site, in the harness's OWN venv)
and relays its ``--json`` event stream into the SAME session's event stream —
so per-site progress, the live task_plan, and the produced sample file all show
up in the conversation (persisted + live) under the same query_id.

The harness lives in a sibling directory (``harness/``) and
emits one JSON object per stdout line; nothing here changes the harness except
the additive ``user_steering.md`` pickup (handled in that repo).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from src.services import run_bundle
from src.services.event_store import (
    load_events,
    load_session,
    max_seq,
    update_session_status,
    upsert_artifact,
)
from src.services.run_registry import RunHandle, run_registry

logger = logging.getLogger(__name__)

# Discovery source_type → harness workflow type (mirror of the bridge's map).
# api sites are STAGED like the rest but LAUNCHED only when credentials are
# present (or the API needs none) — see _awaiting_credentials_info + the gate
# in run_harness_for_source; POST /harness/{site_id}/credentials unlocks them.
_WORKFLOW_FOR = {"embedded": "extraction", "file": "download", "api": "api"}

# Failure-side iteration caps by workflow type (operator decision 2026-06-10:
# api converges in ~1 iter, browser wrangling doesn't apply). Used when the
# caller passes max_iters=None ("per-type default"); an explicit int wins.
_DEFAULT_MAX_ITERS = {"api": 2}
_FALLBACK_MAX_ITERS = 5


def _effective_max_iters(max_iters: int | None, workflow_type: str) -> int:
    if max_iters is not None:
        return max_iters
    return _DEFAULT_MAX_ITERS.get(workflow_type, _FALLBACK_MAX_ITERS)

# Per-host concurrency for harness subprocesses (matches the bridge default).
_HARNESS_CONCURRENCY = 2

# ONE process-global semaphore shared by every explore-loop spawn path (batch
# session runs AND per-source child runs). A per-call Semaphore only capped
# each call's own gather — two discoveries building in parallel stacked
# 2×_HARNESS_CONCURRENCY subprocesses. Lazily created so the primitive binds
# to the running loop, not whatever loop exists at import time.
_harness_sem: asyncio.Semaphore | None = None


def _get_harness_sem() -> asyncio.Semaphore:
    global _harness_sem
    if _harness_sem is None:
        _harness_sem = asyncio.Semaphore(_HARNESS_CONCURRENCY)
    return _harness_sem


# Live harness subprocesses (explore loops AND production runs), so backend
# shutdown can terminate them instead of leaving orphans crawling — and
# writing workspace files — with no supervisor attached.
_live_procs: set[asyncio.subprocess.Process] = set()


def _reap_proc(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill a harness subprocess AND its descendants. Synchronous +
    fully suppressed so it's safe to call from a ``finally`` during task
    cancellation (no await that could itself be re-cancelled and skip the kill).

    Without this, cancelling a run task (session delete, shutdown) only discards
    the proc from the registry and leaks a long-running crawler + headless
    chromium. On Windows the chromium grandchildren do NOT die with the parent,
    so use ``taskkill /T`` to reap the whole tree; elsewhere terminate the proc.
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            subprocess.Popen(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()


async def shutdown_harness_processes(grace_s: float = 5.0) -> None:
    """Terminate every live harness subprocess; SIGKILL stragglers after
    ``grace_s``. Wired into the FastAPI lifespan shutdown."""
    procs = [p for p in list(_live_procs) if p.returncode is None]
    for p in procs:
        with contextlib.suppress(ProcessLookupError, OSError):
            p.terminate()
    if not procs:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*(p.wait() for p in procs), return_exceptions=True),
            timeout=grace_s,
        )
    except asyncio.TimeoutError:
        for p in procs:
            if p.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    p.kill()


def _read_json_file(path: Path) -> dict | None:
    """Sync read+parse of a small machine-written JSON file; None when missing,
    unreadable, or mid-write. Hot watcher loops call it via asyncio.to_thread
    so their sub-second polls never block the event loop on file I/O."""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_new_bytes(path: Path, offset: int) -> tuple[bytes, int]:
    """Read ``path[offset:]`` and return ``(chunk, new_offset)``; ``(b"",
    offset)`` when missing/unreadable. Sync — watcher loops wrap it in
    asyncio.to_thread."""
    try:
        if not path.is_file():
            return b"", offset
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
            return chunk, f.tell()
    except OSError:
        return b"", offset

# Default model for the harness /explore agent (DeepSeek via Anthropic-compat).
_DEFAULT_HARNESS_MODEL = "deepseek-v4-pro"

# How often the per-site watcher re-reads task_plan.md to push real-time updates
# (so the plan shows the moment the agent writes it, not at the iter boundary).
_TASK_PLAN_POLL_SECONDS = 0.8

_bridge = None


def _harness_env() -> dict[str, str]:
    """Env the harness /explore SDK needs — point its Claude CLI at DeepSeek's
    Anthropic-compatible endpoint (same routing the discovery runner uses). The
    harness has no .env and asserts ANTHROPIC_API_KEY at startup, so the spawner
    MUST inject this."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return {
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "zillusion-harness/0.1",
        "CI": "1",
    }


def _load_bridge():
    """Lazily import scripts/discovery_to_harness.py (reuse its staging + paths)."""
    global _bridge
    if _bridge is None:
        bridge_path = (
            Path(__file__).resolve().parents[2].parent / "scripts" / "discovery_to_harness.py"
        )
        spec = importlib.util.spec_from_file_location("discovery_to_harness", bridge_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load bridge at {bridge_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _bridge = mod
    return _bridge


# A site_id / query_id becomes a single path segment under the harness repo.
# Restrict it so a crafted value (``..``, encoded separators like ``%5C``) cannot
# escape the intended root — see site_workspace / site_inputs below.
_SITE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_segment(site_id: str) -> str:
    """Validate site_id is a safe single path segment (anti path-traversal)."""
    if not isinstance(site_id, str) or not _SITE_ID_RE.match(site_id):
        raise ValueError(f"invalid site_id: {site_id!r}")
    return site_id


def site_workspace(site_id: str) -> Path:
    """workspaces/<site_id>/ inside the harness repo."""
    bridge = _load_bridge()
    root = (bridge.HARNESS_DIR / "workspaces").resolve()
    p = (root / _safe_segment(site_id)).resolve()
    p.relative_to(root)  # defense-in-depth: raises ValueError if outside root
    return p


def site_inputs(site_id: str) -> Path:
    """inputs/<site_id>/ inside the harness repo (goal.md + api_spec.json +,
    for keyed APIs, the user-supplied credentials.json)."""
    bridge = _load_bridge()
    root = bridge.HARNESS_INPUTS.resolve()
    p = (root / _safe_segment(site_id)).resolve()
    p.relative_to(root)  # defense-in-depth: raises ValueError if outside root
    return p


def _awaiting_credentials_info(site_id: str) -> dict:
    """Signup/key guidance for an awaiting api site, for the UI card. Prefers
    the bridge-written credentials_needed.json; falls back to api_spec.json's
    signup block. Never raises; never includes a secret."""
    d = site_inputs(site_id)
    out: dict = {
        "write_to": f"inputs/{site_id}/credentials.json",
        "shape": {"api_key": "<your key>", "extra": {}},
    }
    for fname, picker in (
        ("credentials_needed.json", lambda j: j),
        ("api_spec.json", lambda j: {
            "auth_type": ((j.get("auth") or {}).get("type")),
            "signup_url": ((j.get("signup") or {}).get("url")),
            "signup_instructions": ((j.get("signup") or {}).get("instructions")),
            "signup_steps": ((j.get("signup") or {}).get("steps")) or [],
        }),
    ):
        p = d / fname
        if not p.is_file():
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        info = picker(j) or {}
        for k in ("auth_type", "signup_url", "signup_instructions", "signup_steps"):
            if out.get(k) in (None, "", []) and info.get(k) not in (None, "", []):
                out[k] = info[k]
        break  # first readable file wins (needed.json already merged the spec)

    # Provider / homepage / docs for the CredentialsGate fallback ladder (tier 2/3
    # when there's no precise signup_url). These live ONLY in api_spec.json's
    # source/docs blocks (credentials_needed.json doesn't carry them), so read
    # them directly regardless of which file won above.
    try:
        spec = json.loads((d / "api_spec.json").read_text(encoding="utf-8"))
        src = spec.get("source") or {}
        if src.get("provider"):
            out["provider"] = src["provider"]
        if src.get("url"):
            out["source_url"] = src["url"]
        docs = (spec.get("docs") or {}).get("documentation_url")
        # Only surface docs when it points somewhere other than the homepage.
        if docs and docs.rstrip("/").lower() != str(src.get("url") or "").rstrip("/").lower():
            out["docs_url"] = docs
    except (OSError, json.JSONDecodeError):
        pass
    return out


def api_site_ready(site_id: str) -> bool:
    """Launch gate for an api-staged site (delegates to the bridge: ready when
    the API needs no key or inputs/<site>/credentials.json exists)."""
    bridge = _load_bridge()
    return bool(bridge.api_site_ready(site_id, bridge.HARNESS_INPUTS))


async def run_harness_for_session(
    handle: RunHandle,
    query_id: str,
    *,
    site_ids: list[str] | None = None,
    max_iters: int | None = None,
    max_cost_usd: float = 20.0,
    model: str | None = None,
) -> None:
    """Stage the session's crawlable sources and run the harness per site,
    relaying events into ``handle``. Designed to be the body of a RunRegistry
    run (so its events continue the session timeline)."""
    bridge = _load_bridge()

    sess = await load_session(query_id)
    user_query = (sess or {}).get("query_text") or ""

    # Load the discovery FinalReport from the session workspace.
    report_path = (
        Path("agent-workspace/agent-sessions") / query_id / "final_report.json"
    ).resolve()
    if not report_path.is_file():
        await handle.publish("error", {"message": f"no final_report.json for {query_id}", "query_id": query_id})
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))

    ranked: list[dict] = report.get("all_sources_ranked") or []
    # Harness-bound sources (embedded→extraction, file→download, api→api);
    # only unknown types are out of scope.
    crawlable = [s for s in ranked if (s.get("source_type") in _WORKFLOW_FOR)]
    # The frontend "Build scraper" sends DISCOVERY source ids (DataSource.id =
    # sha256(url:type)[:16]); the bridge's site_id is a DIFFERENT scheme
    # (<slug>-<6hex>). Select by the discovery id BEFORE staging — filtering the
    # bridge sid against these ids never matches (→ empty staged → harness_no_sites,
    # which the UI renders as nothing, i.e. "Build scraper does nothing").
    if site_ids is not None:
        want = set(site_ids)
        crawlable = [s for s in crawlable if s.get("id") in want]

    staged: list[tuple[str, dict, str]] = []  # (site_id, source, workflow_type)
    n_stage_failed = 0
    for src in crawlable:
        wf = _WORKFLOW_FOR.get(src.get("source_type"), "extraction")
        try:
            sid = bridge.stage_source(src, user_query, workflow_type=wf)
        except Exception as e:  # noqa: BLE001
            # Surface the skip to the UI — a silently dropped source looked
            # like "Build did nothing" with no trace outside the backend log.
            n_stage_failed += 1
            logger.warning("stage_source failed: %s", e)
            await handle.publish("harness_stage_failed", {
                "query_id": query_id,
                "source_id": src.get("id"),
                "name": src.get("name"),
                "url": src.get("url"),
                "message": f"stage failed: {e}",
            })
            continue
        staged.append((sid, src, wf))
        # Persist site_id → workspace mapping so the steer endpoint can resolve
        # the path even after a restart.
        try:
            await upsert_artifact(
                query_id, kind="harness_site", path=str(site_workspace(sid)), site_id=sid,
                meta={"workflow_type": wf, "name": src.get("name"), "url": src.get("url")},
            )
        except Exception:
            logger.debug("harness_site artifact upsert failed", exc_info=True)

    if not staged:
        msg = (
            f"all {n_stage_failed} crawlable sources failed to stage"
            if n_stage_failed
            else "no crawlable sources to run"
        )
        await handle.publish("harness_no_sites", {"query_id": query_id, "message": msg})
        return

    # api sites without a key are staged but NOT launched: surface the signup
    # guidance and let POST /harness/{site_id}/credentials unlock them.
    runnable: list[tuple[str, dict, str]] = []
    for sid, src, wf in staged:
        if wf == "api" and not api_site_ready(sid):
            await handle.publish("harness_site_awaiting_credentials", {
                "site_id": sid,
                "source_id": src.get("id"),
                "name": src.get("name"),
                "url": src.get("url"),
                "workflow_type": wf,
                "source": src,
                **_awaiting_credentials_info(sid),
            })
            continue
        runnable.append((sid, src, wf))

    await handle.publish("harness_started", {"query_id": query_id, "n_sites": len(runnable)})

    sem = _get_harness_sem()
    await asyncio.gather(
        *(
            _run_site(
                handle, query_id, sid, src, wf, sem,
                _effective_max_iters(max_iters, wf), max_cost_usd, model,
            )
            for (sid, src, wf) in runnable
        )
    )

    await handle.publish("harness_done", {"query_id": query_id, "n_sites": len(runnable)})


def harness_child_id(source: dict) -> str:
    """Deterministic child-session id for a source = the bridge's site_id
    (``<slug>-<6hex>``). Same source → same child session (re-build re-runs it)."""
    return _load_bridge().site_id_for(source)


async def run_harness_for_source(
    handle: RunHandle,
    parent_query_id: str,
    source: dict,
    workflow_type: str,
    *,
    max_iters: int | None = None,
    max_cost_usd: float = 20.0,
    model: str | None = None,
) -> None:
    """Run the harness for ONE discovered source under its OWN child session.

    ``handle.query_id`` IS the child session id (== the bridge site_id); every
    event + artifact attaches to the child, so the scraper gets its own page +
    sidebar entry nested under the discovery parent. Meant to be the body of a
    RunRegistry run keyed by the child id — so several sources build in parallel,
    each its own run."""
    bridge = _load_bridge()
    child_qid = handle.query_id
    parent = await load_session(parent_query_id)
    user_query = (parent or {}).get("query_text") or ""

    try:
        sid = bridge.stage_source(source, user_query, workflow_type=workflow_type)
    except Exception as e:  # noqa: BLE001
        await handle.publish("error", {"message": f"stage_source failed: {e}", "query_id": child_qid})
        return

    try:
        await upsert_artifact(
            child_qid, kind="harness_site", path=str(site_workspace(sid)), site_id=sid,
            meta={"workflow_type": workflow_type, "name": source.get("name"), "url": source.get("url")},
        )
    except Exception:
        logger.debug("harness_site artifact upsert failed", exc_info=True)

    # Keyed api site without its key: surface the signup guidance instead of
    # exploring (a probe without the key just burns an iteration to learn what
    # discovery already suspected). The run ends here; the child session shows
    # the awaiting card, and POST /harness/{site_id}/credentials writes the key
    # then relaunches this same body via restart_child_run.
    if workflow_type == "api" and not api_site_ready(sid):
        await handle.publish("harness_site_awaiting_credentials", {
            "site_id": sid,
            "source_id": source.get("id"),
            "name": source.get("name"),
            "url": source.get("url"),
            "workflow_type": workflow_type,
            "source": source,
            **_awaiting_credentials_info(sid),
        })
        return

    await handle.publish("harness_started", {"query_id": child_qid, "n_sites": 1})
    # Shares the process-global pool: N child builds clicked in parallel queue
    # behind the same _HARNESS_CONCURRENCY cap instead of all spawning at once.
    sem = _get_harness_sem()
    await _run_site(
        handle, child_qid, sid, source, workflow_type, sem,
        _effective_max_iters(max_iters, workflow_type), max_cost_usd, model,
    )
    await handle.publish("harness_done", {"query_id": child_qid, "n_sites": 1})


# ── auto-restart on idle operator feedback (steer endpoint, Phase 1) ──


async def resolve_child_source(site_id: str) -> tuple[str, dict, str] | None:
    """Recover ``(parent_query_id, source, workflow_type)`` for an already-built
    child site from its session row + the parent discovery's ``final_report.json``.

    ``site_id`` IS the child query_id (the bridge ``<slug>-<6hex>``). Returns None
    when the child row, its parent link, or the source in the parent report can't
    be found — i.e. there's nothing to faithfully re-stage."""
    child = await load_session(site_id)
    if child is None:
        return None
    parent_qid = child.get("parent_query_id")
    source_id = child.get("source_id")  # _session_summary extracts this from run_config
    if not parent_qid or not source_id:
        return None
    report_path = (
        Path("agent-workspace/agent-sessions") / parent_qid / "final_report.json"
    ).resolve()
    if not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    grouped = [
        *(report.get("embedded_sources") or []),
        *(report.get("file_sources") or []),
        *(report.get("api_sources") or []),
    ]
    src = next((s for s in grouped if s.get("id") == source_id), None)
    if src is None or src.get("source_type") not in _WORKFLOW_FOR:
        return None
    return parent_qid, src, _WORKFLOW_FOR[src["source_type"]]


async def restart_child_run(
    site_id: str, *, max_iters: int | None = None, max_cost_usd: float = 20.0, model: str | None = None
) -> str | None:
    """Re-run the explore→validate loop for a terminal/idle child site, mirroring
    a 'Build scraper' re-click.

    The site's accumulated workspace artifacts (task_plan.md, iter_summary.md,
    user_steering.md, workflow.py) are PRESERVED — re-staging only rewrites
    ``inputs/<id>/{goal,seed}`` — and re-injected at the new iter-1 SessionStart
    (the existing carry-forward). Returns the child query_id if a run was
    (re)started, or None if a live run already exists for this site OR the source
    can't be resolved (caller keeps the feedback filed for a manual build)."""
    existing = run_registry.get(site_id)
    if existing is not None and not existing.is_terminal:
        return None  # already live — caller should live-steer, not restart
    resolved = await resolve_child_source(site_id)
    if resolved is None:
        return None
    parent_qid, source, wf = resolved
    try:
        await update_session_status(site_id, "running")
    except Exception:  # noqa: BLE001
        logger.debug("restart_child_run: status flip failed", exc_info=True)
    await run_registry.start(
        site_id,
        lambda h: run_harness_for_source(
            h, parent_qid, source, wf,
            max_iters=max_iters, max_cost_usd=max_cost_usd, model=model,
        ),
        start_seq=await max_seq(site_id),
    )
    return site_id


async def _emit_site_artifacts(handle: RunHandle, query_id: str, site_id: str, *, seen: set[str]) -> None:
    """Emit/refresh the output SAMPLE for a site (idempotent; reducer upserts).

    task_plan is NOT emitted here — it's pushed in real time the moment the agent
    writes/edits it, by the per-site watcher in `_run_site`
    (`_emit_task_plan_if_changed`), so it appears immediately rather than only at
    an iteration boundary."""
    ws = site_workspace(site_id)
    sample = ws / "output_sample.json"
    if sample.is_file() and f"sample:{site_id}" not in seen:
        try:
            aid = await upsert_artifact(
                query_id, kind="harness_output_sample", path=str(sample), site_id=site_id,
            )
            await handle.publish("harness_sample_ready", {
                "site_id": site_id, "artifact_id": aid,
                "filename": "output_sample.json", "content_type": "json",
            })
            seen.add(f"sample:{site_id}")
        except Exception:
            logger.debug("sample emit failed", exc_info=True)


# ── live activity: one /explore (or /validate) turn → a compact UI ping ──

_ACTIVITY_TEXT_CAP = 200


def _activity_from_turn(inner: Any, stage: str) -> dict | None:
    """Map one explore/validate turn event (the nested run.py event) to a compact
    activity ping for the scraper page's live indicator — mirrors the discovery
    `activity` model (assistant narration → thinking, tool call → executing).
    Returns None for events we don't surface (tool results, system boot, run
    result — too large / noisy). Text is capped so the per-event DB row + stream
    payload stay small."""
    if not isinstance(inner, dict):
        return None
    k = inner.get("kind")
    if k == "assistant_text":
        text = (inner.get("text") or "").strip()
        if not text:
            return None
        return {"phase": "thinking", "stage": stage, "text": text[:_ACTIVITY_TEXT_CAP]}
    if k == "tool_use":
        name = inner.get("name") or "tool"
        return {"phase": "executing", "stage": stage, "text": str(name)[:_ACTIVITY_TEXT_CAP]}
    return None


def _read_task_plan_if_changed(site_id: str, last_text: str | None) -> str | None:
    """Return task_plan.md's content iff it exists, is non-empty, and differs from
    `last_text`; else None. Pure (read + dedup only, no publish) so the watcher's
    change-detection is unit-testable."""
    tp = site_workspace(site_id) / "task_plan.md"
    if not tp.is_file():
        return None
    try:
        text = tp.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip() or text == last_text:
        return None
    return text


async def _run_site(
    handle: RunHandle,
    query_id: str,
    site_id: str,
    source: dict,
    workflow_type: str,
    sem: asyncio.Semaphore,
    max_iters: int,
    max_cost_usd: float,
    model: str | None,
) -> None:
    bridge = _load_bridge()
    seen: set[str] = set()
    async with sem:
        await handle.publish("harness_site_started", {
            "site_id": site_id,
            "source_id": source.get("id"),
            "name": source.get("name"),
            "url": source.get("url"),
            "workflow_type": workflow_type,
            # Full discovered source so the scraper child session can render the
            # source card (migrated from the discovery page).
            "source": source,
        })

        effective_model = model or _DEFAULT_HARNESS_MODEL
        argv: list[str] = [
            str(bridge.HARNESS_PY), "-m", "runtime.cli",
            "--model", effective_model, "--json",
            "explore-loop", site_id,
            "--max-iters", str(max_iters), "--max-cost-usd", str(max_cost_usd),
        ]

        env = {
            **os.environ, **_harness_env(),
            "CRAWLER_EXPLORER_ROOT": str(bridge.HARNESS_DIR),
            # Web-UI run: a human IS reachable via the takeover WS, so the
            # harness streams its headed login browser instead of giving up
            # (autonomous-blocked). See routes/takeover.py + _takeover_watcher.
            "CRAWLER_EXPLORER_TAKEOVER": "1",
        }
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(bridge.HARNESS_DIR),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    # Raise the StreamReader ceiling from the 64 KiB default. The
                    # read loop below chunks manually and no longer relies on this,
                    # but a higher limit also relaxes flow-control back-pressure on
                    # large output bursts. See the read loop for the full rationale.
                    limit=2**24,  # 16 MiB
                ),
                timeout=30.0,  # a wedged spawn (stale venv path, AV scan) fails fast
            )
        except Exception as e:  # noqa: BLE001
            await handle.publish("harness_site_done", {"site_id": site_id, "error": f"spawn failed: {e}"})
            return
        _live_procs.add(proc)

        final_verdict = None
        agentic_run_id: str | None = None
        assert proc.stdout is not None

        async def _handle_line(line: str) -> None:
            """Parse one NDJSON line from the harness and relay the events the
            UI needs. ``nonlocal final_verdict`` so a terminating ``loop_done``
            is visible to the post-loop completion publish below."""
            nonlocal final_verdict, agentic_run_id
            if not line:
                return
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                return  # stderr/log noise merged into the stream
            kind = ev.get("kind")
            if kind == "iter_start":
                await handle.publish("harness_iter_start", {"site_id": site_id, "iter_n": ev.get("iter_n")})
            elif kind == "iter_done":
                await handle.publish("harness_iter_done", {
                    "site_id": site_id, "iter_n": ev.get("iter_n"),
                    "verdict": ev.get("verdict"), "cost_usd": ev.get("cost_usd"),
                })
                # Refresh task_plan (deduped) + sample at the iteration boundary;
                # the watcher already streams task_plan changes mid-iteration.
                await _emit_task_plan_if_changed()
                await _emit_site_artifacts(handle, query_id, site_id, seen=seen)
            elif kind == "crawl_done":
                # The explore loop dispatched the agentic crawl agent inline and
                # it finished — remember its run id so the post-loop terminal
                # registration can locate runs/<run_id>/ without scanning disk.
                agentic_run_id = ev.get("run_id") or agentic_run_id
            elif kind == "loop_done":
                final_verdict = ev.get("final_verdict")
                await handle.publish("harness_site_done", {
                    "site_id": site_id,
                    "final_verdict": final_verdict,
                    "exit_reason": ev.get("exit_reason"),
                    "total_cost_usd": ev.get("total_cost_usd"),
                })
            elif kind in ("explore_event", "validate_event"):
                # Per-turn agent activity → the scraper page's live indicator
                # (mirrors discovery's ephemeral `activity`). Curated + capped in
                # _activity_from_turn; tool results / system boots are dropped.
                act = _activity_from_turn(
                    ev.get("event"),
                    stage="explore" if kind == "explore_event" else "validate",
                )
                if act is not None:
                    await handle.publish(
                        "harness_activity",
                        {"site_id": site_id, "iter_n": ev.get("iter_n"), **act},
                    )
            # `snapshot` is intentionally NOT relayed (too noisy); iteration
            # markers + task_plan + sample + activity give the UI what it needs.

        # ── real-time task_plan watcher ─────────────────────────────────
        # The /explore agent writes task_plan.md early in iter 1 (and edits it as
        # the plan evolves). Poll it so the UI shows the plan the moment it lands
        # and refreshes on every change — not just at iteration boundaries.
        # Deduped on content so unchanged polls are no-ops.
        last_tp_text: str | None = None

        async def _emit_task_plan_if_changed() -> None:
            nonlocal last_tp_text
            # to_thread: this runs on a 0.8s poll cadence; the file read must
            # not block the event loop (slow disk = stalled SSE for everyone).
            text = await asyncio.to_thread(_read_task_plan_if_changed, site_id, last_tp_text)
            if text is None:
                return
            last_tp_text = text
            try:
                tp = site_workspace(site_id) / "task_plan.md"
                aid = await upsert_artifact(query_id, kind="harness_task_plan", path=str(tp), site_id=site_id)
                await handle.publish("harness_task_plan", {"site_id": site_id, "text": text, "artifact_id": aid})
            except Exception:  # noqa: BLE001
                logger.debug("task_plan emit failed", exc_info=True)

        async def _task_plan_watcher() -> None:
            while True:
                await asyncio.sleep(_TASK_PLAN_POLL_SECONDS)
                try:
                    await _emit_task_plan_if_changed()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.debug("task_plan watcher poll failed", exc_info=True)

        # ── human-takeover watcher ──────────────────────────────────────
        # The harness writes workspaces/<site>/_takeover_request.json when it
        # needs the user to log in / solve a captcha in the streamed login
        # browser (browser_request_user_login takeover mode). The harness<->
        # backend reverse channel is file-based, so poll it and relay
        # request/done as SSE — the sub-session shows + tears down the takeover
        # canvas, and routes/takeover.py bridges the live CDP screencast+input.
        req_path = site_workspace(site_id) / "_takeover_request.json"

        async def _takeover_watcher() -> None:
            announced_at: str | None = None
            while True:
                await asyncio.sleep(0.7)
                data = await asyncio.to_thread(_read_json_file, req_path)
                if data and data.get("status") == "pending":
                    if data.get("created_at") != announced_at:
                        announced_at = data.get("created_at")
                        await handle.publish("harness_takeover_request", {
                            "site_id": site_id,
                            "reason": data.get("reason"),
                            "page_url": data.get("page_url"),
                            "message": data.get("message"),
                            "wall_type": data.get("wall_type"),
                        })
                elif announced_at is not None:
                    announced_at = None
                    await handle.publish("harness_takeover_done", {"site_id": site_id})

        # ── agent → user message watcher ────────────────────────────────
        # The explore agent's send_user_message tool appends one JSON line per
        # message to workspaces/<site>/_agent_messages.jsonl. Tail it (byte
        # offset + newline framing, so a half-written final line is never
        # consumed) and relay each NEW line as a persisted harness_agent_message
        # — a left-aligned chat bubble. Start at end-of-file so a re-run of a
        # PRESERVED workspace does not re-emit a prior run's messages. The drain
        # is shared with the final sweep so a message written in the last poll
        # window (just before the subprocess exits) is not lost.
        msg_path = site_workspace(site_id) / "_agent_messages.jsonl"
        try:
            _msg_start_offset = msg_path.stat().st_size if msg_path.is_file() else 0
        except OSError:
            _msg_start_offset = 0
        _msg_state: dict[str, Any] = {"offset": _msg_start_offset, "pending": b""}

        async def _drain_agent_messages() -> None:
            chunk, _msg_state["offset"] = await asyncio.to_thread(
                _read_new_bytes, msg_path, _msg_state["offset"]
            )
            if not chunk:
                return
            _msg_state["pending"] += chunk
            while b"\n" in _msg_state["pending"]:
                raw, _msg_state["pending"] = _msg_state["pending"].split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                text = (rec.get("message") or "").strip()
                if text:
                    await handle.publish("harness_agent_message", {
                        "site_id": site_id,
                        "text": text,
                        "created_at": rec.get("created_at"),
                    })

        async def _agent_message_watcher() -> None:
            while True:
                await asyncio.sleep(0.7)
                try:
                    await _drain_agent_messages()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.debug("agent message watcher poll failed", exc_info=True)

        takeover_task = asyncio.create_task(_takeover_watcher())
        task_plan_task = asyncio.create_task(_task_plan_watcher())
        agent_msg_task = asyncio.create_task(_agent_message_watcher())

        # Read the NDJSON stream in fixed-size chunks and split on newlines
        # ourselves. We deliberately avoid `async for line in proc.stdout`
        # (and readline()/readuntil()): asyncio's StreamReader caps a single
        # line at `limit` — 64 KiB by default — and raises
        # ValueError("Separator is not found, and chunk exceed the limit") when
        # one harness event line exceeds it (a snapshot/explore_event carrying
        # page HTML, or a long traceback merged in via stderr=STDOUT). That
        # exception used to escape this loop and surface in the UI as a red
        # error box. Manual chunking has no per-line ceiling.
        try:
            buf = b""
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    await _handle_line(raw.decode("utf-8", errors="replace").strip())
            # Flush a final line emitted without a trailing newline.
            await _handle_line(buf.decode("utf-8", errors="replace").strip())

            await proc.wait()
        finally:
            # Reap BEFORE discard: if the task was cancelled (session delete /
            # shutdown), proc is still alive here — kill its tree so we never
            # leak an orphaned crawler + chromium. No-op once it has exited.
            _reap_proc(proc)
            _live_procs.discard(proc)
            takeover_task.cancel()
            task_plan_task.cancel()
            agent_msg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await takeover_task
            with contextlib.suppress(asyncio.CancelledError):
                await task_plan_task
            with contextlib.suppress(asyncio.CancelledError):
                await agent_msg_task
        # Final sweep: catch the last task_plan edit (watcher is stopped now) +
        # the sample (may have appeared on the last iteration) + any agent
        # message written in the final poll window.
        await _emit_task_plan_if_changed()
        await _emit_site_artifacts(handle, query_id, site_id, seen=seen)
        # Inline/agentic routes deliver their dataset DURING explore (no Run
        # phase follows) — register it + publish the run event sequence so the
        # download chips appear. No-op for deterministic/undeclared routes.
        with contextlib.suppress(Exception):
            await _emit_terminal_dataset(handle, site_id, agentic_run_id)
        with contextlib.suppress(Exception):
            await _drain_agent_messages()
        if final_verdict is None:
            await handle.publish("harness_site_done", {"site_id": site_id, "exit_code": proc.returncode})


# ── Run (production crawl) phase — the third stage after explore→validate ──
# Spawns `runtime.cli run <site_id>` (the VALIDATED workflow.py at FULL scope,
# output KEPT), relays the run agent's narration + the gate-computed outcome,
# and pushes a live crawl-progress ticker by re-reading the deterministic core's
# runs/<run_id>/_run_progress.json (run_exec.py flushes it ~1s; we re-publish a
# compact, deduped subset every _RUN_PROGRESS_POLL_SECONDS). run_id is assigned
# HERE — not by the harness — so the watcher knows the file path from second one.

_RUN_PROGRESS_POLL_SECONDS = 3.0
_RUN_ACTIVITY_TEXT_CAP = 200

# Default crawl kill-thresholds (mirror runtime.run_exec defaults).
_RUN_WALL_CLOCK_CAP_S = 3600.0
_RUN_STALL_TIMEOUT_S = 300.0

# Mirror of runtime.run_agent.OUTCOME_LINE_RE — parsed backend-side so we never
# import the harness package (its deps live only in the harness venv).
# Brackets are OPTIONAL: on a real run (2026-06-09) deepseek emitted the line
# verbatim except for the literal [] and a COMPLETE run displayed as ABORTED.
# The `<site> run_id=` anchor right after keeps the bare form unambiguous.
_RUN_OUTCOME_RE = re.compile(
    r"\[?(COMPLETE|PARTIAL|FAILED|ABORTED)\]?\s+(\S+)\s+run_id=(\S+)"
    r"(?:\s+records=(\d+))?\s*(?:[—-]\s*(.*))?",
    re.IGNORECASE,
)


async def run_production_for_source(
    handle: RunHandle,
    site_id: str,
    *,
    crawl_mode: str = "full",
    wall_clock_cap_s: float = _RUN_WALL_CLOCK_CAP_S,
    stall_timeout_s: float = _RUN_STALL_TIMEOUT_S,
    model: str | None = None,
    steer_resume_offset: int | None = None,
) -> None:
    """Run the VALIDATED workflow.py at full scope for an already-built site, as
    the body of a RunRegistry run keyed by the child id (``handle.query_id`` ==
    ``site_id``). Emits ``run_*`` events into the SAME per-session timeline as the
    explore→validate loop, so the run continues on the same conversation page.

    ``steer_resume_offset``: when this run was started BY a steer on an idle run
    (the chat-on-the-run-page auto-rerun), the message was appended to
    user_run_steering.md before the subprocess exists — pass the pre-append file
    size so the run agent's tail starts THERE and delivers it (its default is
    EOF-at-boot, which would skip the message)."""
    # Mark the phase on the live handle so the EXPLORE steer endpoint can tell
    # "a run is in flight" apart from "the explore loop is live" (its message
    # file is only read by /explore, so it must not claim live delivery here).
    handle.phase = "run"  # type: ignore[attr-defined]
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await handle.publish("run_started", {
        "site_id": site_id, "run_id": run_id, "crawl_mode": crawl_mode,
    })
    # Guarantee a terminal event pairs the run_started: if _run_production_site
    # raises before it emits run_done/run_error, an orphan run_started would make
    # last_loop_verdict() see an "in-flight" run forever and 409 every future
    # POST /run. Emit run_error so the segment is properly terminated.
    try:
        await _run_production_site(
            handle, site_id, run_id,
            crawl_mode=crawl_mode,
            wall_clock_cap_s=wall_clock_cap_s,
            stall_timeout_s=stall_timeout_s,
            model=model,
            steer_resume_offset=steer_resume_offset,
        )
    except asyncio.CancelledError:
        # Task cancellation (shutdown / session delete): the subprocess is reaped
        # in _run_production_site's finally. Re-raise — do NOT swallow cancellation.
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("production run crashed for %s (run %s): %s", site_id, run_id, e)
        await handle.publish("run_error", {
            "site_id": site_id, "run_id": run_id,
            "message": f"run crashed: {type(e).__name__}: {e}",
        })


def _run_activity_from_event(ev: dict) -> dict | None:
    """Map one run-agent NDJSON event to a compact run_activity ping (assistant
    narration → thinking, tool call → executing). Returns None for events we
    don't surface (tool results / system boot / result), and for the machine
    outcome-contract line (shown via run_done, not as narration)."""
    k = ev.get("kind")
    if k == "assistant_text":
        text = (ev.get("text") or "").strip()
        if not text or _RUN_OUTCOME_RE.search(text):
            return None
        return {"phase": "thinking", "text": text[:_RUN_ACTIVITY_TEXT_CAP]}
    if k == "tool_use":
        name = ev.get("name") or "tool"
        return {"phase": "executing", "text": str(name)[:_RUN_ACTIVITY_TEXT_CAP]}
    return None


def _manifest_outcome(site_id: str, run_id: str) -> str | None:
    """The gate-COMPUTED ``outcome:`` from runs/<run_id>/manifest.yaml — the
    authoritative completion verdict (the agent's outcome line merely restates
    it). Line-scan instead of a YAML parse so the backend needs no yaml dep;
    the file is machine-written (top-level ``outcome: complete``; dimension
    lines are indented so startswith() can't hit them)."""
    p = site_workspace(site_id) / "runs" / run_id / "manifest.yaml"
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith("outcome:"):
                val = ln.split(":", 1)[1].strip().strip("'\"").upper()
                if val in ("COMPLETE", "PARTIAL", "FAILED", "ABORTED"):
                    return val
    except OSError:
        pass
    return None


def _manifest_failed_reason(site_id: str, run_id: str) -> str | None:
    """A one-line reason assembled from the manifest's FAILING gating dimensions
    (``dim: basis; dim2: basis2``) — used for run_done when the agent's outcome
    line was missing/unparseable, so the UI never shows a bare FAILED chip with
    no text. Same dependency-free line scan as ``_manifest_outcome``; the file
    is machine-written (``dimensions:`` block, 2/4-space indents; a wrapped
    basis keeps only its first line — fine for a one-liner)."""
    p = site_workspace(site_id) / "runs" / run_id / "manifest.yaml"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    parts: list[str] = []
    dim: str | None = None
    status: str | None = None
    basis: str | None = None
    gating = False

    def _flush() -> None:
        nonlocal dim, status, basis, gating
        if dim and gating and status == "fail":
            parts.append(f"{dim}: {basis or 'failed'}")
        dim = status = basis = None
        gating = False

    in_dims = False
    for ln in lines:
        if ln.startswith("dimensions:"):
            in_dims = True
            continue
        if not in_dims:
            continue
        if ln.strip() and not ln.startswith("  "):
            break  # left the dimensions block (a new top-level key)
        m = re.match(r"^  (\w+):\s*$", ln)
        if m:
            _flush()
            dim = m.group(1)
            continue
        sm = re.match(r"^    status:\s*(\S+)", ln)
        if sm:
            status = sm.group(1).strip("'\"")
            continue
        gm = re.match(r"^    gating:\s*(\S+)", ln)
        if gm:
            gating = gm.group(1).lower() == "true"
            continue
        bm = re.match(r"^    basis:\s*(.+)$", ln)
        if bm:
            basis = bm.group(1).strip().strip("'\"")
            continue
    _flush()
    if not parts:
        return None
    return "; ".join(parts)[:300]


def _manifest_record_count(site_id: str, run_id: str) -> int | None:
    """Top-level ``record_count:`` from runs/<run_id>/manifest.yaml — same
    dependency-free line scan as ``_manifest_outcome`` (dimension lines are
    indented, so startswith() can't hit them)."""
    p = site_workspace(site_id) / "runs" / run_id / "manifest.yaml"
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith("record_count:"):
                val = ln.split(":", 1)[1].strip().strip("'\"")
                return int(val) if val.isdigit() else None
    except OSError:
        pass
    return None


async def _register_run_dataset(
    handle: RunHandle,
    site_id: str,
    run_id: str,
    out_file: Path | None,
    bundle_dir: Path | None,
    record_count: int | None,
) -> None:
    """Register the kept dataset as the site's downloadable artifact and publish
    ``run_output_ready``. A multi-file deliverable (``bundle_dir``, or an
    ``out_file`` with on-disk companions) registers the run DIRECTORY — the
    download endpoint zips it on request; a lone output.json stays a direct
    link. Shared by the Run-agent watcher and the inline/agentic terminal path."""
    if bundle_dir is None and out_file is not None and run_bundle.should_bundle(
        out_file.parent, out_file
    ):
        bundle_dir = out_file.parent
    if bundle_dir is not None:
        try:
            aid = await upsert_artifact(
                handle.query_id, kind="harness_run_output", path=str(bundle_dir),
                site_id=site_id,
                meta={"run_id": run_id, "record_count": record_count, "bundle": True},
            )
            await handle.publish("run_output_ready", {
                "site_id": site_id,
                "run_id": run_id,
                "artifact_id": aid,
                "filename": f"{site_id}_{run_id}.zip",
                "content_type": "zip",
                # Pre-zip total — the zip is built per request, so this is the
                # honest size signal available now.
                "bytes": run_bundle.total_bytes(run_bundle.iter_bundle_files(bundle_dir)),
                "record_count": record_count,
            })
        except Exception:  # noqa: BLE001
            logger.debug("run bundle artifact emit failed", exc_info=True)
    elif out_file is not None:
        try:
            aid = await upsert_artifact(
                handle.query_id, kind="harness_run_output", path=str(out_file),
                site_id=site_id, meta={"run_id": run_id, "record_count": record_count},
            )
            await handle.publish("run_output_ready", {
                "site_id": site_id,
                "run_id": run_id,
                "artifact_id": aid,
                "filename": out_file.name,
                "content_type": "json",
                "bytes": out_file.stat().st_size,
                "record_count": record_count,
            })
        except Exception:  # noqa: BLE001
            logger.debug("run output artifact emit failed", exc_info=True)


def _latest_run_with_output(site_id: str, prefix: str) -> str | None:
    """Newest runs/<run_id>/ (by output.json mtime) whose id starts with
    ``prefix`` — disk fallback when the run id wasn't captured from the event
    stream (e.g. backend restarted mid-explore)."""
    runs_dir = site_workspace(site_id) / "runs"
    best: tuple[float, str] | None = None
    try:
        for out in runs_dir.glob("*/output.json"):
            rid = out.parent.name
            if not rid.startswith(prefix):
                continue
            mt = out.stat().st_mtime
            if best is None or mt > best[0]:
                best = (mt, rid)
    except OSError:
        return None
    return best[1] if best else None


async def _emit_terminal_dataset(
    handle: RunHandle, site_id: str, agentic_run_id: str | None
) -> None:
    """After an explore loop ends: if its terminal route was INLINE or AGENTIC,
    the dataset already exists under runs/<run_id>/ with no Run-agent phase to
    register it — register it now and publish the standard run event sequence
    (run_started → run_output_ready [→ run_field_doc_ready] → run_done) so the
    existing RunPanel shows the download chips. Deterministic routes return
    untouched (their dataset arrives via POST /harness/{site_id}/run).

    Idempotent enough for re-runs: upsert_artifact updates in place and the
    reducer overwrites the same chip; last_loop_verdict skips the completed
    started→done segment, so the re-run gate is unaffected."""
    route_info = _read_json_file(site_workspace(site_id) / "crawl_route.json") or {}
    route = route_info.get("route")
    if route not in ("inline", "agentic"):
        return
    run_id = (
        route_info.get("run_id")
        if route == "inline"
        else (agentic_run_id or _latest_run_with_output(site_id, "run-"))
    )
    if not run_id:
        return
    run_dir = site_workspace(site_id) / "runs" / str(run_id)
    out_file = run_dir / "output.json"
    if not out_file.is_file():
        return  # nothing produced (e.g. crawl aborted before any commit)

    # Field-doc snapshot — mirror the Run agent: copy the workspace task_plan.md
    # into the run dir so the field doc ships in the bundle AND as its own chip.
    field_doc: Path | None = None
    tp = site_workspace(site_id) / "task_plan.md"
    snap = run_dir / "task_plan.md"
    try:
        if snap.is_file():
            field_doc = snap
        elif tp.is_file():
            snap.write_text(tp.read_text(encoding="utf-8"), encoding="utf-8")
            field_doc = snap
    except OSError:
        field_doc = None

    record_count = _manifest_record_count(site_id, str(run_id))
    await handle.publish("run_started", {
        "site_id": site_id, "run_id": run_id, "crawl_mode": route,
    })
    await _register_run_dataset(handle, site_id, str(run_id), out_file, None, record_count)
    if field_doc is not None:
        try:
            aid = await upsert_artifact(
                handle.query_id, kind="harness_run_field_doc", path=str(field_doc),
                site_id=site_id, meta={"run_id": run_id},
            )
            await handle.publish("run_field_doc_ready", {
                "site_id": site_id,
                "run_id": run_id,
                "artifact_id": aid,
                "filename": "task_plan.md",
                "content_type": "markdown",
            })
        except Exception:  # noqa: BLE001
            logger.debug("terminal field doc emit failed", exc_info=True)
    outcome = _manifest_outcome(site_id, str(run_id))
    await handle.publish("run_done", {
        "site_id": site_id,
        "run_id": run_id,
        # PARTIAL, not COMPLETE, when the manifest can't confirm — data exists
        # (output.json checked above) but completion is unverified.
        "outcome": outcome or "PARTIAL",
        "record_count": record_count,
        "reason": None if outcome else "outcome unavailable from the run manifest",
    })


def _run_progress_payload(site_id: str, run_id: str, prog: dict) -> dict:
    """Compact subset of run_exec's _run_progress.json for the UI ticker."""
    return {
        "site_id": site_id,
        "run_id": run_id,
        "state": prog.get("state"),
        "record_count": prog.get("record_count"),
        "lines_emitted": prog.get("lines_emitted"),
        "elapsed_s": prog.get("elapsed_s"),
        # Publish-time server clock: the frontend ticks its elapsed display
        # locally between deduped snapshots, and this anchors a replayed
        # snapshot to wall time instead of its (stale) arrival time.
        "at": time.time(),
        "last_output_age_s": prog.get("last_output_age_s"),
    }


async def _run_production_site(
    handle: RunHandle,
    site_id: str,
    run_id: str,
    *,
    crawl_mode: str,
    wall_clock_cap_s: float,
    stall_timeout_s: float,
    model: str | None,
    steer_resume_offset: int | None = None,
) -> None:
    bridge = _load_bridge()

    # Snapshot the field documentation AS IT STANDS at run start: task_plan.md
    # (the explore-written plan whose "目标数据与字段" table documents every
    # output field) is MUTABLE — a later explore re-run rewrites it — but the
    # dataset this run produces should ship with the field semantics it was
    # crawled under. Registered as a downloadable artifact immediately, so the
    # doc is available while the crawl is still running.
    try:
        tp = site_workspace(site_id) / "task_plan.md"
        if tp.is_file():
            snap_dir = site_workspace(site_id) / "runs" / run_id
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap = snap_dir / "task_plan.md"
            snap.write_text(tp.read_text(encoding="utf-8"), encoding="utf-8")
            aid = await upsert_artifact(
                handle.query_id, kind="harness_run_field_doc", path=str(snap),
                site_id=site_id, meta={"run_id": run_id},
            )
            await handle.publish("run_field_doc_ready", {
                "site_id": site_id,
                "run_id": run_id,
                "artifact_id": aid,
                "filename": "task_plan.md",
                "content_type": "markdown",
                "bytes": snap.stat().st_size,
            })
    except Exception:  # noqa: BLE001
        logger.debug("run field-doc snapshot failed", exc_info=True)
    effective_model = model or _DEFAULT_HARNESS_MODEL
    argv: list[str] = [
        str(bridge.HARNESS_PY), "-m", "runtime.cli",
        "--model", effective_model, "--json",
        "run", site_id,
        "--crawl-mode", crawl_mode,
        "--wall-clock-cap", str(wall_clock_cap_s),
        "--stall-timeout", str(stall_timeout_s),
        "--run-id", run_id,
    ]
    if steer_resume_offset is not None:
        argv += ["--steer-offset", str(steer_resume_offset)]
    env = {
        **os.environ, **_harness_env(),
        "CRAWLER_EXPLORER_ROOT": str(bridge.HARNESS_DIR),
        "CRAWLER_EXPLORER_TAKEOVER": "1",
    }
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=str(bridge.HARNESS_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                limit=2**24,  # 16 MiB — same readline-ceiling rationale as _run_site
            ),
            timeout=30.0,  # same fail-fast rationale as _run_site
        )
    except Exception as e:  # noqa: BLE001
        await handle.publish("run_error", {"site_id": site_id, "run_id": run_id, "message": f"spawn failed: {e}"})
        return
    _live_procs.add(proc)

    assert proc.stdout is not None
    narration: list[str] = []  # assistant_text chunks → parse the final outcome line
    run_err: str | None = None

    async def _handle_line(line: str) -> None:
        nonlocal run_err
        if not line:
            return
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return  # stderr/log noise merged into the stream
        if not isinstance(ev, dict):
            return
        kind = ev.get("kind")
        if kind == "assistant_text":
            narration.append(ev.get("text") or "")
        elif kind == "error":
            # Surfaced once at the end (run_error) so there's a single terminal
            # event; don't also emit it as an activity ping.
            run_err = ev.get("error") or run_err
            return
        act = _run_activity_from_event(ev)
        if act is not None:
            await handle.publish("run_activity", {"site_id": site_id, "run_id": run_id, **act})

    # ── human-takeover watcher ──────────────────────────────────────────
    # The Run agent's request_user_takeover tool writes the SAME workspace-root
    # _takeover_request.json handshake the explore phase uses — but pointing at
    # the WORKFLOW'S OWN live browser (WORKFLOW_CDP_PORT contract) instead of a
    # fresh login browser. Poll + relay it identically; the WS bridge
    # (routes/takeover.py) and the frontend takeover window are shared as-is.
    req_path = site_workspace(site_id) / "_takeover_request.json"

    async def _takeover_watcher() -> None:
        announced_at: str | None = None
        while True:
            await asyncio.sleep(0.7)
            data = await asyncio.to_thread(_read_json_file, req_path)
            if data and data.get("status") == "pending":
                if data.get("created_at") != announced_at:
                    announced_at = data.get("created_at")
                    await handle.publish("harness_takeover_request", {
                        "site_id": site_id,
                        "reason": data.get("reason"),
                        "page_url": data.get("page_url"),
                        "message": data.get("message"),
                        "wall_type": data.get("wall_type"),
                    })
            elif announced_at is not None:
                announced_at = None
                await handle.publish("harness_takeover_done", {"site_id": site_id})

    # ── agent → user message watcher ────────────────────────────────────
    # The Run agent's send_user_message tool appends one {message, created_at}
    # JSON line to runs/<run_id>/_agent_messages.jsonl (run-scoped, unlike
    # explore's workspace-root file — the run writes ONLY under runs/<run_id>/).
    # Tail it (byte offset + newline framing, so a half-written final line is
    # never consumed) and relay each line as a persisted harness_agent_message —
    # the same event the explore phase uses, so the frontend renders it as a
    # left-aligned chat bubble with zero new handling. Offset starts at 0: the
    # runs/<run_id>/ dir is fresh per run, so there's nothing stale to replay.
    msg_path = site_workspace(site_id) / "runs" / run_id / "_agent_messages.jsonl"
    _msg_state: dict[str, Any] = {"offset": 0, "pending": b""}

    async def _drain_run_agent_messages() -> None:
        chunk, _msg_state["offset"] = await asyncio.to_thread(
            _read_new_bytes, msg_path, _msg_state["offset"]
        )
        if not chunk:
            return
        _msg_state["pending"] += chunk
        while b"\n" in _msg_state["pending"]:
            raw, _msg_state["pending"] = _msg_state["pending"].split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            text = (rec.get("message") or "").strip()
            if text:
                await handle.publish("harness_agent_message", {
                    "site_id": site_id,
                    "run_id": run_id,
                    "text": text,
                    "created_at": rec.get("created_at"),
                })

    async def _agent_message_watcher() -> None:
        while True:
            await asyncio.sleep(0.7)
            try:
                await _drain_run_agent_messages()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("run agent message watcher poll failed", exc_info=True)

    # ── live crawl-progress ticker ──────────────────────────────────────
    progress_path = site_workspace(site_id) / "runs" / run_id / "_run_progress.json"

    async def _progress_watcher() -> None:
        last_fp: tuple | None = None
        while True:
            await asyncio.sleep(_RUN_PROGRESS_POLL_SECONDS)
            prog = await asyncio.to_thread(_read_json_file, progress_path)
            if prog is None:
                continue
            # Dedup identical polls (idle crawl) on the signals that actually move.
            fp = (prog.get("state"), prog.get("lines_emitted"), prog.get("record_count"))
            if fp == last_fp:
                continue
            last_fp = fp
            await handle.publish("run_progress", _run_progress_payload(site_id, run_id, prog))

    progress_task = asyncio.create_task(_progress_watcher())
    agent_msg_task = asyncio.create_task(_agent_message_watcher())
    takeover_task = asyncio.create_task(_takeover_watcher())
    try:
        buf = b""
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                await _handle_line(raw.decode("utf-8", errors="replace").strip())
        await _handle_line(buf.decode("utf-8", errors="replace").strip())
        await proc.wait()
    finally:
        # Reap BEFORE discard: a cancelled run task leaves proc alive here —
        # kill its tree (crawler + chromium) instead of orphaning it. No-op
        # once it has exited normally.
        _reap_proc(proc)
        _live_procs.discard(proc)
        progress_task.cancel()
        agent_msg_task.cancel()
        takeover_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task
        with contextlib.suppress(asyncio.CancelledError):
            await agent_msg_task
        with contextlib.suppress(asyncio.CancelledError):
            await takeover_task

    # A takeover left pending by an abrupt agent death would wedge the frontend
    # window and confuse a later run's watcher (stale announce). Clean up ONLY
    # this run's own handshake (phase/run_id guard — never clobber an explore
    # handshake) and signal done; publishing done with nothing announced is a
    # harmless no-op in the reducer.
    try:
        stale = _read_json_file(req_path)
        if stale and stale.get("phase") == "run" and stale.get("run_id") == run_id:
            req_path.unlink(missing_ok=True)
            await handle.publish("harness_takeover_done", {"site_id": site_id})
    except OSError:
        pass

    # Final agent-message drain (watcher stopped) so a message written in the
    # last poll window — e.g. the agent's wrap-up right before exiting — lands.
    with contextlib.suppress(Exception):
        await _drain_run_agent_messages()

    # Final progress sweep (watcher stopped) so the terminal state lands.
    final_progress: dict | None = _read_json_file(progress_path)
    if final_progress is not None:
        await handle.publish("run_progress", _run_progress_payload(site_id, run_id, final_progress))

    # Gate-computed outcome from the agent's final outcome line.
    outcome: str | None = None
    record_count: int | None = None
    reason: str | None = None
    for ln in reversed("\n".join(narration).splitlines()):
        m = _RUN_OUTCOME_RE.search(ln.strip())
        if m:
            outcome = m.group(1).upper()
            record_count = int(m.group(4)) if m.group(4) else None
            reason = (m.group(5) or "").strip() or None
            break
    # No parseable outcome line from a CLEAN session → consult the manifest:
    # its outcome is gate-computed and the line is only a restatement of it.
    # (A machinery error without a line still goes down the run_error path —
    # an early-dead agent's half-filled manifest must not masquerade as a
    # normal result.)
    if outcome is None and run_err is None:
        outcome = _manifest_outcome(site_id, run_id)
        if outcome is not None and reason is None:
            # No reason text either (the line never parsed) — assemble one from
            # the manifest's failing gating dims so the panel isn't a bare chip.
            reason = _manifest_failed_reason(site_id, run_id) or (
                "outcome from manifest (no parseable outcome line)"
            )
    if record_count is None and final_progress is not None:
        record_count = final_progress.get("record_count")

    # Register the kept dataset as a downloadable artifact. Extraction runs
    # produce runs/<run_id>/output.json (final_progress's output_path tracks a
    # potential --output-root relocation); a MULTI-FILE deliverable — a
    # download-type run (output_path is a dir) or output.json with on-disk
    # companions (media/, weak-schema file_ref targets) — registers the run
    # DIRECTORY instead, which the download endpoint zips on request.
    # Emitted before the terminal event, and on the error path too: a crawl
    # that produced data before the agent machinery failed is still data.
    out_file: Path | None = None
    bundle_dir: Path | None = None
    if final_progress is not None and final_progress.get("output_path"):
        p = Path(str(final_progress["output_path"]))
        if p.is_file():
            out_file = p
        elif p.is_dir() and run_bundle.should_bundle(p, None):
            bundle_dir = p
    if out_file is None and bundle_dir is None:
        p = site_workspace(site_id) / "runs" / run_id / "output.json"
        if p.is_file():
            out_file = p
    await _register_run_dataset(handle, site_id, run_id, out_file, bundle_dir, record_count)

    if outcome is None and run_err is not None:
        # The run session machinery errored and never emitted an outcome line.
        await handle.publish("run_error", {"site_id": site_id, "run_id": run_id, "message": run_err})
    else:
        await handle.publish("run_done", {
            "site_id": site_id,
            "run_id": run_id,
            "outcome": outcome or "ABORTED",
            "record_count": record_count,
            "reason": reason or (run_err if outcome is None else None),
            "exit_code": proc.returncode,
        })


async def last_loop_verdict(site_id: str) -> str | None:
    """The verdict of the most recent explore→validate loop for a child site, by
    scanning its persisted timeline backwards: the first ``harness_site_done``
    wins. Returns None when the loop is currently (re)running
    (``harness_site_started`` seen first) or a Run is IN FLIGHT (``run_started``
    with no terminal) — anything that should BLOCK starting a run. A TERMINATED
    run segment (run_started…run_done/run_error) is skipped over: a completed
    production run does not invalidate the PASS that preceded it, so the site
    can be run again (data refresh) without re-exploring."""
    events = await load_events(site_id, 0)
    run_terminals = 0
    for ev in reversed(events):
        et = ev.get("event_type")
        if et in ("run_done", "run_error"):
            run_terminals += 1
        elif et == "run_started":
            if run_terminals == 0:
                # No matching terminal. Either a run is genuinely in flight, OR a
                # run crashed without emitting one (backend died mid-run — no code
                # ran to publish run_error). Distinguish by the live registry: a
                # live non-terminal handle ⇒ really in flight (block). Otherwise
                # it's a dead orphan ⇒ skip it and keep walking back to the PASS,
                # so a backend crash mid-run doesn't 409-lock re-runs forever.
                live = run_registry.get(site_id)
                if live is not None and not live.is_terminal:
                    return None  # genuinely in-flight — don't start a second one
                continue  # crashed/orphan run_started — treat as terminated
            run_terminals -= 1  # this segment terminated; keep walking back
        elif et == "harness_site_done":
            return (ev.get("payload") or {}).get("final_verdict")
        elif et == "harness_site_started":
            return None
    return None
