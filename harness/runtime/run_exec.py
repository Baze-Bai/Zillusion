"""Deterministic streaming crawl executor — the Run agent's core.

The production analog of ``validator_checks.run_workflow_isolated``: it runs the
validated ``workflow.py`` FOR REAL (full scope, output KEPT), and streams the
subprocess so the agent can monitor a long-running crawl and so hard kill
thresholds are enforced even if the LLM never polls.

Key differences from the validator's isolated run:
  - ``CRAWL_MODE=full`` (the validator runs the default ``sample``).
  - Output is KEPT under ``runs/<run_id>/`` (the validator's rerun/ is throwaway).
  - The subprocess runs as a BACKGROUND asyncio task; the agent's MCP tools
    (start_crawl / poll_crawl) read its live signals between turns.
  - Hard thresholds (wall-clock cap, stall timeout) are enforced autonomously.

**CRITICAL — chunked stdout read, NOT ``readline``.** ``asyncio``'s
``StreamReader.readline`` raises ``LimitOverrunError`` on any single stdout line
> 64 KiB (page HTML dump / a traceback), which would silently kill monitoring
(see ``memory/reference_harness_stdout_readline_limit.md`` — the harness already
hit this in ``backend/.../harness_orchestrator.py`` and fixed it the same way).
We read fixed 64 KiB chunks and split lines ourselves, so a multi-MB single line
is buffered, not fatal. stderr is merged into stdout (``stderr=STDOUT``) for a
single deadlock-free pipe; tracebacks interleave into the log and are scanned by
``ran_clean``.

Control files (manifest / report / feedback / progress / log) always live under
``workspaces/<id>/runs/<run_id>/`` (E:, discoverable by the SessionStart hook).
With ``--output-root`` the bulky deliverable (output.json + media/) lands under
that root instead (E: runs near-full — see ``memory/preference_disk_scratch_on_D``);
control stays on E:.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.subprocess_safety import scrubbed_env
from runtime.validator_checks import PROJECT_ROOT, _workspace, _load_records, resolve_auth_path


# ── tunables ─────────────────────────────────────────────────────────
CHECK_INTERVAL_S = 5.0  # wake cadence for threshold checks when no output
# Aligned with the backend's 3s poll of _run_progress.json (operator decision
# 2026-06-10; was 1.0 — 2 of every 3 writes were never read by anyone).
FLUSH_INTERVAL_S = 3.0  # throttle for _run_progress.json + log appends
TAIL_LINES = 60  # stdout tail kept in memory / surfaced to the agent
READ_CHUNK = 65536  # 64 KiB chunked read (NOT readline)
STREAM_LIMIT = 2**24  # 16 MiB StreamReader buffer (well above 64 KiB lines)
TERM_GRACE_S = 5.0  # wait after terminate() before kill()

DEFAULT_WALL_CLOCK_CAP_S = 3600  # 1h
DEFAULT_STALL_TIMEOUT_S = 600  # 10m

# Wake-condition tunables. Deterministic detectors (pump-side) append alerts;
# a blocked poll_with_wait returns EARLY when a NEW alert lands — event-grade
# latency inside pull mechanics (the agent is single-threaded: a push could not
# reach it while it's blocked in a poll anyway).
STALL_WARN_FRACTION = 0.5  # alert at this fraction of stall_timeout_s silence
PATTERN_ALERT_MIN_GAP_S = 10.0  # debounce: ≥ this gap between pattern alerts
ALERTS_MAX = 50  # in-memory alert ring (alerts_total keeps the true count)


def detect_workflow_type(site_id: str) -> str:
    """Workflow type from workspace artifacts, api > download > extraction,
    with an input-side api fallback (matches runtime.validate._detect_workflow_type)."""
    ws = _workspace(site_id)
    if (ws / "api_manifest.yaml").exists():
        return "api"
    if (ws / "download_manifest.yaml").exists():
        return "download"
    if (PROJECT_ROOT / "inputs" / site_id / "api_spec.json").exists():
        return "api"
    return "extraction"


def _free_port() -> int:
    """Grab a free localhost TCP port for the workflow browser's
    --remote-debugging-port (mirrors mcp_server.browser._free_port)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ── run handle + registry ────────────────────────────────────────────


@dataclass
class RunHandle:
    site_id: str
    run_id: str
    control_dir: Path  # workspaces/<id>/runs/<run_id>/ (always E:, discoverable)
    work_dir: Path  # where workflow runs + writes output (control_dir or --output-root)
    workflow_type: str
    crawl_mode: str
    wall_clock_cap_s: float
    stall_timeout_s: float
    proc: Any = None
    pump_task: Any = None
    started_monotonic: float = 0.0
    state: str = "starting"  # starting | running | done | killed | error
    exit_code: int | None = None
    killed_reason: str | None = None
    error: str | None = None
    lines_emitted: int = 0
    last_output_monotonic: float = 0.0
    record_count: int | None = None
    output_path: str | None = None
    last_status: dict | None = None
    stdout_tail: deque = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    _log_buf: list[str] = field(default_factory=list)
    _last_flush: float = 0.0
    # CDP port injected into the workflow env (WORKFLOW_CDP_PORT). The workflow
    # CONTRACT (hypothesis-loop skill) passes it to chromium so a human takeover
    # can attach to the LIVE crawl browser. None capability ≠ port dead — a
    # pre-contract / browserless workflow simply never opens it.
    cdp_port: int | None = None
    # While a human takeover is pending, the kill thresholds are SUSPENDED (the
    # user may take minutes on a captcha; the workflow may be stalled on the
    # wall the whole time). end_takeover() shifts the clocks so paused time
    # counts against neither the wall-clock cap nor the stall timeout.
    takeover_active: bool = False
    _takeover_started: float = 0.0
    # Wake conditions: the pump appends alert dicts ({n, kind, detail,
    # elapsed_s}); poll_with_wait returns early when alerts_total grows past
    # its entry baseline. wake_regex is the agent-declared stdout pattern
    # (set via poll_crawl's wake_pattern; persists across polls).
    alerts: deque = field(default_factory=lambda: deque(maxlen=ALERTS_MAX))
    alerts_total: int = 0
    wake_regex: Any = None
    _stall_warned: bool = False
    _last_pattern_alert: float = 0.0


_ACTIVE_RUNS: dict[tuple[str, str], RunHandle] = {}


def _key(site_id: str, run_id: str) -> tuple[str, str]:
    return (site_id, run_id)


# ── path setup ───────────────────────────────────────────────────────


def _ensure_auth(work_dir: Path) -> None:
    """Stage the owning user's auth_state.json into work_dir so workflow.py's
    walk-up from __file__ finds it locally. Under multi-tenant prod the cookies
    live at workspaces/_auth/<uid>/auth_state.json (resolve_auth_path) — NEVER on
    work_dir's parent chain — so a login-gated workflow would otherwise run
    UNauthenticated. (Also covers the off-tree --output-root case the old
    project-global walk-up could not reach.)"""
    try:
        src = resolve_auth_path()
        dst = work_dir / "auth_state.json"
        if src.exists() and src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
    except OSError:
        pass


def _stage_inputs(site_id: str, work_dir: Path) -> None:
    """Copy the inputs workflow.py needs into work_dir (explore artifacts stay
    read-only). Mirrors validator_checks.run_workflow_isolated's copy step."""
    ws = _workspace(site_id)
    for fname in (
        "workflow.py",
        "helpers.py",
        "selectors.yaml",
        "download_manifest.yaml",
        "api_manifest.yaml",
    ):
        src = ws / fname
        if src.exists():
            shutil.copy2(src, work_dir / fname)


def _ensure_credentials(site_id: str, work_dir: Path) -> None:
    """api workflows: copy the user-supplied inputs/<site>/credentials.json
    next to workflow.py. Unlike the project-global auth_state.json,
    inputs/<site>/ is never on work_dir's parent chain, so the workflow's
    walk-up only finds the key via this local copy (mirrors
    validator_checks.run_workflow_isolated)."""
    try:
        src = PROJECT_ROOT / "inputs" / site_id / "credentials.json"
        if src.exists():
            shutil.copy2(src, work_dir / "credentials.json")
    except OSError:
        pass


# ── lifecycle ────────────────────────────────────────────────────────


async def start_crawl(
    site_id: str,
    run_id: str,
    *,
    crawl_mode: str = "full",
    wall_clock_cap_s: float = DEFAULT_WALL_CLOCK_CAP_S,
    stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
    python_exe: str | None = None,
    output_root: str | Path | None = None,
) -> dict:
    """Spawn workflow.py (CRAWL_MODE=<crawl_mode>) as a background task in an
    isolated runs/<run_id>/ workdir and start streaming it. Returns immediately;
    poll_crawl/get_progress read the live signals."""
    k = _key(site_id, run_id)
    if k in _ACTIVE_RUNS and _ACTIVE_RUNS[k].state in ("starting", "running"):
        return {"started": False, "reason": "a run with this id is already active"}

    ws = _workspace(site_id)
    wf = ws / "workflow.py"
    if not wf.exists():
        return {"started": False, "reason": "workflow.py not found"}

    workflow_type = detect_workflow_type(site_id)
    control_dir = ws / "runs" / run_id
    work_dir = (Path(output_root) / site_id / "runs" / run_id) if output_root else control_dir
    control_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    _stage_inputs(site_id, work_dir)
    _ensure_auth(work_dir)
    _ensure_credentials(site_id, work_dir)

    h = RunHandle(
        site_id=site_id,
        run_id=run_id,
        control_dir=control_dir,
        work_dir=work_dir,
        workflow_type=workflow_type,
        crawl_mode=crawl_mode,
        wall_clock_cap_s=wall_clock_cap_s,
        stall_timeout_s=stall_timeout_s,
    )
    # WORKFLOW_CDP_PORT: contract-compliant workflows pass this to chromium's
    # --remote-debugging-port, letting a human takeover attach to the LIVE crawl
    # browser (headless is fine — the backend streams a CDP screencast). Old /
    # browserless workflows ignore it; request_user_takeover probes the port and
    # degrades gracefully.
    cdp_port = _free_port()
    # workflow.py is agent-written + untrusted — scrub the harness's secrets from
    # its env (the per-site credential is staged as credentials.json in cwd, not
    # read from env). The non-secret crawl knobs are re-added via `extra`.
    env = scrubbed_env(extra={
        "CRAWL_MODE": crawl_mode,
        # Headed production crawl when CRAWLER_EXPLORER_HEADLESS=0 (some sites
        # detect headless Chromium and serve stripped HTML). The generated
        # workflow.py reads HEADLESS (see the hypothesis-loop contract).
        "HEADLESS": os.environ.get("CRAWLER_EXPLORER_HEADLESS", "1"),
        "WORKFLOW_CDP_PORT": str(cdp_port),
    })
    h.cdp_port = cdp_port
    py = python_exe or sys.executable
    try:
        proc = await asyncio.create_subprocess_exec(
            py,
            "workflow.py",
            cwd=str(work_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge — single deadlock-free pipe
            limit=STREAM_LIMIT,
        )
    except OSError as exc:
        h.state = "error"
        h.error = f"spawn failed: {type(exc).__name__}: {exc}"
        _ACTIVE_RUNS[k] = h
        _write_progress(h)
        return {"started": False, "reason": h.error}

    now = time.monotonic()
    h.proc = proc
    h.started_monotonic = now
    h.last_output_monotonic = now
    h._last_flush = now
    h.state = "running"
    _ACTIVE_RUNS[k] = h
    _write_progress(h)

    h.pump_task = asyncio.create_task(_pump(h))
    return {
        "started": True,
        "run_id": run_id,
        "pid": proc.pid,
        "workflow_type": workflow_type,
        "crawl_mode": crawl_mode,
        "control_dir": str(control_dir),
        "work_dir": str(work_dir),
    }


async def _pump(h: RunHandle) -> None:
    """Stream the subprocess: chunked-read stdout, split lines ourselves (dodges
    the 64 KiB readline limit), enforce thresholds, flush progress. Never lets an
    exception escape silently — it lands in h.error / h.state."""
    try:
        buf = b""
        assert h.proc is not None
        while True:
            try:
                chunk = await asyncio.wait_for(
                    h.proc.stdout.read(READ_CHUNK), timeout=CHECK_INTERVAL_S
                )
            except asyncio.TimeoutError:
                chunk = b"__tick__"  # no data this interval — fall through to threshold check
            if chunk == b"":
                break  # EOF — subprocess closed stdout
            if chunk != b"__tick__":
                buf += chunk
                *complete, buf = buf.split(b"\n")
                if complete:
                    _on_lines(h, [ln.decode("utf-8", "replace") for ln in complete])

            _check_stall_warning(h)
            reason = _check_thresholds(h)
            if reason:
                await _kill(h, reason)
                break
            now = time.monotonic()
            if now - h._last_flush >= FLUSH_INTERVAL_S:
                _flush(h)

        # drain a trailing partial line (no terminating newline)
        if buf.strip():
            _on_lines(h, [buf.decode("utf-8", "replace")])

        if h.proc.returncode is None:
            try:
                await asyncio.wait_for(h.proc.wait(), timeout=TERM_GRACE_S)
            except asyncio.TimeoutError:
                await _kill(h, h.killed_reason or "did not exit after stdout EOF")
        h.exit_code = h.proc.returncode
        if h.state != "killed":
            h.state = "done"
    except Exception as exc:  # noqa: BLE001 — surface, never swallow
        h.state = "error"
        h.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    finally:
        try:
            _post_process(h)
        except Exception as exc:  # noqa: BLE001
            h.error = (h.error or "") + f" | post_process: {type(exc).__name__}: {exc}"
        _flush(h)


def _on_lines(h: RunHandle, lines: list[str]) -> None:
    h.last_output_monotonic = time.monotonic()
    h._stall_warned = False  # output resumed — re-arm the stall warning
    for ln in lines:
        h.lines_emitted += 1
        h.stdout_tail.append(ln)
        h._log_buf.append(ln)
        if ln.lstrip().startswith("Traceback (most recent call last"):
            _alert(h, "traceback", ln.strip()[:200])
        rx = h.wake_regex
        if rx is not None and rx.search(ln):
            now = time.monotonic()
            if now - h._last_pattern_alert >= PATTERN_ALERT_MIN_GAP_S:
                h._last_pattern_alert = now
                _alert(h, "pattern", ln.strip()[:200])


def _alert(h: RunHandle, kind: str, detail: str) -> None:
    h.alerts_total += 1
    h.alerts.append(
        {
            "n": h.alerts_total,
            "kind": kind,
            "detail": detail,
            "elapsed_s": round(time.monotonic() - h.started_monotonic, 1),
        }
    )


def _check_stall_warning(h: RunHandle) -> None:
    """ONE stall_warning alert per silence stretch, at STALL_WARN_FRACTION of
    the stall timeout — early enough that the agent (woken inside its blocking
    poll) can still diagnose / takeover / kill before the hard stall kill."""
    if h.takeover_active or h._stall_warned:
        return
    silent = time.monotonic() - h.last_output_monotonic
    if silent > h.stall_timeout_s * STALL_WARN_FRACTION:
        h._stall_warned = True
        _alert(
            h,
            "stall_warning",
            f"no output for {silent:.0f}s (hard stall kill at {h.stall_timeout_s:.0f}s)",
        )


def _check_thresholds(h: RunHandle) -> str | None:
    if h.takeover_active:
        return None  # a human is operating the crawl browser — never kill under them
    now = time.monotonic()
    if now - h.started_monotonic > h.wall_clock_cap_s:
        return f"wall_clock_cap exceeded ({h.wall_clock_cap_s:.0f}s)"
    if now - h.last_output_monotonic > h.stall_timeout_s:
        return f"stall_timeout: no output for {h.stall_timeout_s:.0f}s"
    return None


def begin_takeover(h: RunHandle) -> None:
    """Suspend the kill thresholds while a human takeover is pending."""
    if not h.takeover_active:
        h.takeover_active = True
        h._takeover_started = time.monotonic()


def end_takeover(h: RunHandle) -> None:
    """Resume the kill thresholds. The paused interval counts against NEITHER
    clock: started_monotonic shifts forward by the pause (wall-clock cap), and
    the stall timer restarts fresh (the workflow was likely silent on the wall
    the whole time — give it a full window to resume output)."""
    if h.takeover_active:
        h.takeover_active = False
        paused = time.monotonic() - h._takeover_started
        h.started_monotonic += paused
        h.last_output_monotonic = time.monotonic()


async def _kill(h: RunHandle, reason: str) -> None:
    h.killed_reason = reason
    h.state = "killed"
    proc = h.proc
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=TERM_GRACE_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except ProcessLookupError:
        pass
    h.exit_code = proc.returncode


async def kill_crawl(site_id: str, run_id: str, reason: str) -> dict:
    """Deliberate agent-initiated abort of a RUNNING crawl — the LLM's kill
    lever (the wall-clock / stall kills stay autonomous in _pump). Terminates
    the subprocess, then waits briefly for the pump to drain + post-process so
    the returned snapshot already reflects any incrementally-flushed partial
    output (record_count / output_path)."""
    h = _ACTIVE_RUNS.get(_key(site_id, run_id))
    if h is None:
        return {"killed": False, "reason": "no active run with this id in this process"}
    if h.state not in ("starting", "running"):
        return {
            "killed": False,
            "reason": f"crawl already terminal (state={h.state})",
            "state": h.state,
        }
    if h.takeover_active:
        return {
            "killed": False,
            "reason": "a human takeover is in progress — never kill under the user",
        }
    await _kill(h, f"agent kill: {(reason or '').strip() or 'unspecified'}")
    if h.pump_task is not None:
        try:  # shield: wait for the pump's drain/post_process, never cancel it
            await asyncio.wait_for(asyncio.shield(h.pump_task), timeout=10.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — snapshot below either way
            pass
    return {
        "killed": True,
        "state": h.state,
        "killed_reason": h.killed_reason,
        "exit_code": h.exit_code,
        "record_count": h.record_count,
        "output_path": h.output_path,
    }


def _find_misplaced_output(work_dir: Path) -> Path | None:
    """Contract-drift salvage: workflow.py wrote its output somewhere BELOW
    work_dir instead of at the root (e.g. ``output/output_sample.json`` —
    the fabricators-of-create case). The data is real; find it so the
    deliverable can still be materialized at the canonical spot.

    Preference order: output_sample.json over output.json (contract name
    first), then shallower over deeper, then newer mtime. Root-level files
    are the caller's business; control paths (any `_`-prefixed part) and
    *.tmp partials are never candidates."""
    best: tuple[tuple[int, int, float], Path] | None = None
    try:
        for name_rank, pattern in ((0, "output_sample.json"), (1, "output.json")):
            for p in work_dir.rglob(pattern):
                rel = p.relative_to(work_dir)
                if len(rel.parts) == 1:  # root level — caller handles directly
                    continue
                if any(part.startswith("_") for part in rel.parts):
                    continue
                if not p.is_file():
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0.0
                key = (name_rank, len(rel.parts), -mtime)
                if best is None or key < best[0]:
                    best = (key, p)
    except OSError:
        return None  # unreadable tree → behave as before (no salvage)
    return best[1] if best else None


def _count_records(path: Path) -> int | None:
    """len() of the record list at ``path``; None when unreadable / not a
    record list — a bad count must never cost us the output_path itself."""
    try:
        records = _load_records(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return len(records) if records is not None else None


def _materialize(src: Path, out: Path) -> bool:
    """Copy ``src`` to the canonical ``out`` via a .tmp sibling + atomic
    replace. A half-written canonical file (ENOSPC mid-copy on the
    chronically near-full workspace drive) must never exist under the real
    name — the backend's fallback registration would ship it as the
    deliverable while its presence suppresses the real data in the bundle.
    (.tmp names are excluded by run_bundle and _list_produced both.)"""
    tmp = out.with_name(out.name + ".tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, out)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _post_process(h: RunHandle) -> None:
    """After exit: read _last_run_status.json, materialize the deliverable
    (extraction: output_sample.json -> output.json + record_count), record paths."""
    status_path = h.work_dir / "_last_run_status.json"
    if status_path.exists():
        try:
            h.last_status = json.loads(status_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            h.last_status = None

    if h.workflow_type == "download":
        # No single output file — the deliverable is the downloaded file(s).
        h.output_path = str(h.work_dir)
        return

    src = h.work_dir / "output_sample.json"
    out = h.work_dir / "output.json"

    def _adopt(source: Path) -> None:
        if _materialize(source, out):
            h.output_path = str(out)
            h.record_count = _count_records(out)
        else:
            h.output_path = str(source)  # still a real file → still downloadable
            h.record_count = _count_records(source)

    if src.is_file():
        _adopt(src)  # contract path: root sample is the authority
    elif out.is_file():
        # workflow.py wrote output.json at the root directly (no sample) —
        # accept it as the deliverable as-is. Checked BEFORE any salvage so a
        # stray subdirectory sample can never clobber a root deliverable.
        h.output_path = str(out)
        h.record_count = _count_records(out)
    else:
        # Salvage a misplaced deliverable (contract drift). The validator-side
        # hard gate keeps NEW workflows honest; this keeps EXISTING drifted
        # ones downloadable — data the crawl produced is data the user gets.
        # out does not exist on this branch, so materializing cannot overwrite.
        stray = _find_misplaced_output(h.work_dir)
        if stray is not None:
            _adopt(stray)


# ── progress serialization ───────────────────────────────────────────


def _progress_dict(h: RunHandle) -> dict:
    now = time.monotonic()
    running = h.state in ("starting", "running")
    return {
        "site_id": h.site_id,
        "run_id": h.run_id,
        "state": h.state,
        "workflow_type": h.workflow_type,
        "crawl_mode": h.crawl_mode,
        "elapsed_s": round(now - h.started_monotonic, 1) if h.started_monotonic else 0.0,
        "lines_emitted": h.lines_emitted,
        "last_output_age_s": round(now - h.last_output_monotonic, 1) if running else None,
        "exit_code": h.exit_code,
        "killed_reason": h.killed_reason,
        "error": h.error,
        "record_count": h.record_count,
        "output_path": h.output_path,
        "last_status": h.last_status,
        "stdout_tail": list(h.stdout_tail),
        "wall_clock_cap_s": h.wall_clock_cap_s,
        "stall_timeout_s": h.stall_timeout_s,
        "cdp_port": h.cdp_port,
        "takeover_active": h.takeover_active,
        "alerts_total": h.alerts_total,
        "alerts_recent": list(h.alerts)[-5:],
    }


def _write_progress(h: RunHandle) -> None:
    # Atomic (tmp + replace): the backend polls this file from another
    # process — an in-place write_text could be observed half-written
    # (JSONDecodeError there means a silently dropped progress tick), and a
    # crash mid-write would leave a corrupt file for the final sweep.
    try:
        p = h.control_dir / "_run_progress.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(_progress_dict(h), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(p)
    except OSError:
        pass


def _flush(h: RunHandle) -> None:
    """Append buffered stdout to crawl_stdout.log + rewrite _run_progress.json."""
    if h._log_buf:
        try:
            with (h.control_dir / "crawl_stdout.log").open("a", encoding="utf-8") as f:
                f.write("\n".join(h._log_buf) + "\n")
        except OSError:
            pass
        h._log_buf.clear()
    _write_progress(h)
    h._last_flush = time.monotonic()


# ── reads for the MCP tools ──────────────────────────────────────────


def get_progress(site_id: str, run_id: str) -> dict:
    """Live signals from the in-memory handle if present (same process as the
    agent), else the last-persisted _run_progress.json on disk."""
    h = _ACTIVE_RUNS.get(_key(site_id, run_id))
    if h is not None:
        return _progress_dict(h)
    p = _workspace(site_id) / "runs" / run_id / "_run_progress.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"state": "unknown", "reason": "no active run and no _run_progress.json"}


def get_handle(site_id: str, run_id: str) -> RunHandle | None:
    return _ACTIVE_RUNS.get(_key(site_id, run_id))


async def cleanup_run(site_id: str, run_id: str) -> None:
    """Best-effort: terminate a still-running crawl + cancel its pump. Called by
    the agent driver's finally-block so an early-exiting agent doesn't leave a
    full crawl (and its headless chromium) running unattended."""
    h = _ACTIVE_RUNS.get(_key(site_id, run_id))
    if h is None:
        return
    if h.proc is not None and h.proc.returncode is None:
        await _kill(h, "cleanup: agent session ended before crawl finished")
    if h.pump_task is not None and not h.pump_task.done():
        h.pump_task.cancel()
        try:
            await h.pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


__all__ = [
    "start_crawl",
    "kill_crawl",
    "get_progress",
    "get_handle",
    "cleanup_run",
    "detect_workflow_type",
    "begin_takeover",
    "end_takeover",
    "RunHandle",
    "DEFAULT_WALL_CLOCK_CAP_S",
    "DEFAULT_STALL_TIMEOUT_S",
    "STALL_WARN_FRACTION",
]
