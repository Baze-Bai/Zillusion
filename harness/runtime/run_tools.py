"""SDK MCP tool surface for the Run agent.

The production analog of ``runtime.validator_tools``: wraps the streaming crawl
executor (``run_exec``) + the run-manifest/report/feedback writers into an
in-process SDK MCP server. This is the agent's ENTIRE capability surface — no
general Bash/Write/Edit, so the Run agent cannot modify an explore artifact
(isolation by tool surface, same discipline as the validator).

Writes target ``workspaces/<site>/runs/<run_id>/`` only:
  - manifest.yaml   (gate-computed outcome)
  - report.md       (narrative)
  - feedback.yaml   (hypothesis feedback for the next /explore)
  - output.json / media/ (the kept deliverable — written by workflow.py)
  - _run_progress.json / crawl_stdout.log (written by run_exec)

The hard kill thresholds (wall-clock cap, stall timeout, --output-root) are
injected via ``build_runner_server(config)`` — the AGENT does not own them; it
just calls ``start_crawl`` and the deterministic core enforces the limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from runtime import run_exec
from runtime.validator_checks import _workspace, _load_records, resolve_auth_path

# Reuse the validator's MCP result helpers verbatim (_ok wraps a dict as MCP
# text content; _safe makes a raised handler exception a recoverable result;
# _schema builds an explicit JSON Schema so OPTIONAL params aren't force-required).
from runtime.validator_tools import _ok, _safe, _schema
from mcp_server.schemas import RunManifestFile, new_run_manifest


def _run_dir(site_id: str, run_id: str) -> Path:
    """Control dir — ALWAYS under workspaces/<id>/runs/<run_id>/ (E:, so the
    SessionStart hook + manifest discovery find it even when --output-root sends
    the bulky output elsewhere)."""
    return _workspace(site_id) / "runs" / run_id


# ── manifest / report / feedback backends (runs/<run_id>/ only) ──────


def init_run_manifest(
    site_id: str,
    run_id: str,
    workflow_type: str = "extraction",
    crawl_mode: str = "full",
    data_definition: dict | None = None,
) -> dict:
    m = new_run_manifest(
        run_id, site_id, workflow_type, crawl_mode, data_definition=data_definition
    )
    d = _run_dir(site_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    m.save(d / "manifest.yaml")
    return {
        "created": f"runs/{run_id}/manifest.yaml",
        "dimensions": sorted(m.dimensions),
        "outcome": m.outcome,
    }


def update_run_manifest(
    site_id: str,
    run_id: str,
    dim: str,
    status: str,
    basis: str = "",
    evidence: str = "",
    record_count: int | None = None,
    output_path: str = "",
) -> dict:
    p = _run_dir(site_id, run_id) / "manifest.yaml"
    # Hard floor: secrets_safe can NEVER be recorded pass/n-a while a real
    # session-secret leak sits in the shipped artifacts. Re-derive from disk and
    # ignore the caller's claim (the agentic agent can call this tool directly) —
    # mirrors the validator's secrets_safe floor; spoof-proof by construction.
    if dim == "secrets_safe" and status in ("pass", "n/a"):
        from runtime.secret_scan import scan_run_secrets

        res = scan_run_secrets(_run_dir(site_id, run_id))
        if not res.get("na") and not res.get("ok"):
            files = ", ".join(sorted({leak["file"] for leak in res.get("leaks") or []}))
            return {
                "rejected": True,
                "dim": dim,
                "reason": f"secrets_safe must be fail: session-secret literal leaked into {files}",
                "required": "record secrets_safe as fail",
            }
    m = RunManifestFile.load(p)
    m.set_dimension(dim, status, basis=basis or None, evidence=evidence or None)
    if record_count is not None:
        m.record_count = record_count
    if output_path:
        m.output_path = output_path
    m.save(p)
    return {"dim": dim, "status": status, "outcome": m.outcome}


def read_run_manifest(site_id: str, run_id: str) -> dict:
    m = RunManifestFile.load(_run_dir(site_id, run_id) / "manifest.yaml")
    return {
        "outcome": m.outcome,
        "record_count": m.record_count,
        "output_path": m.output_path,
        "dimensions": {
            n: {"status": d.status, "gating": d.gating, "evidence": d.evidence}
            for n, d in m.dimensions.items()
        },
    }


def write_run_report(site_id: str, run_id: str, section: str) -> dict:
    d = _run_dir(site_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    rp = d / "report.md"
    prev = rp.read_text(encoding="utf-8") if rp.exists() else "# Run report\n\n"
    rp.write_text(prev + section.rstrip() + "\n\n", encoding="utf-8")
    return {"written": f"runs/{run_id}/report.md"}


def append_feedback(
    site_id: str,
    run_id: str,
    claim: str,
    basis: str = "",
    priority: str = "medium",
    notes: str = "",
) -> dict:
    """Append a hypothesis-feedback item to runs/<run_id>/feedback.yaml — same
    shape the validator writes + the Explore agent already reads."""
    d = _run_dir(site_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    fp = d / "feedback.yaml"
    data = yaml.safe_load(fp.read_text(encoding="utf-8")) if fp.exists() else None
    items = data if isinstance(data, list) else []
    items.append({"claim": claim, "basis": basis, "priority": priority, "notes": notes})
    fp.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"appended": claim, "total": len(items)}


def send_user_message(site_id: str, run_id: str, message: str) -> dict:
    """Append one operator-facing message to runs/<run_id>/_agent_messages.jsonl
    — the same append-only reverse channel the explore agent uses (the backend
    tails it and shows each line as a chat bubble), but run-scoped so the Run
    agent keeps its writes-ONLY-runs/<run_id>/ isolation. Schema mirrors
    mcp_server.server.send_user_message: one {message, created_at} per line."""
    msg = (message or "").strip()
    if not msg:
        return {"sent": False, "reason": "empty message"}
    d = _run_dir(site_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "message": msg,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with (d / "_agent_messages.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"sent": True, "chars": len(msg)}


# ── output reading ───────────────────────────────────────────────────


_MAX_POLL_WAIT_S = 300.0  # clamp a single checkpoint's server-side wait


async def poll_with_wait(
    site_id: str, run_id: str, wait_s: float = 0.0, wake_pattern: str | None = None
) -> dict:
    """Return the crawl's live signals. If ``wait_s`` > 0, block up to that long
    (yielding to the background pump every 0.5s) and return EARLY the moment the
    state goes terminal OR a NEW alert lands (stall warning at half the stall
    timeout, a traceback line, a stdout line matching ``wake_pattern``) — event
    latency inside the blocking poll, without busy-polling the turn budget.
    ``wake_pattern`` (optional regex, ``(?i)`` for case-insensitive) persists on
    the handle across polls. The response carries ``wake_reason`` (terminal |
    alert | timeout) and, on an alert wake, ``new_alerts``."""
    h = run_exec.get_handle(site_id, run_id)
    pattern_error: str | None = None
    if wake_pattern:
        if h is not None:
            try:
                h.wake_regex = re.compile(wake_pattern)
            except re.error as exc:
                pattern_error = f"invalid wake_pattern (ignored): {exc}"
        else:
            pattern_error = "wake_pattern ignored: no live run handle in this process"
    baseline = h.alerts_total if h is not None else 0
    end = time.monotonic() + max(0.0, min(wait_s, _MAX_POLL_WAIT_S))
    while True:
        prog = run_exec.get_progress(site_id, run_id)
        terminal = prog.get("state") in ("done", "killed", "error", "unknown")
        alerted = (prog.get("alerts_total") or 0) > baseline
        if terminal or alerted or time.monotonic() >= end:
            prog["wake_reason"] = "terminal" if terminal else ("alert" if alerted else "timeout")
            if alerted and h is not None:
                prog["new_alerts"] = [a for a in list(h.alerts) if a.get("n", 0) > baseline]
            if pattern_error:
                prog["wake_pattern_error"] = pattern_error
            return prog
        await asyncio.sleep(0.5)


def read_crawl_output(site_id: str, run_id: str) -> dict:
    """Parse the kept deliverable (extraction: output.json; download: the
    declared files). Call after the crawl reaches a terminal state."""
    prog = run_exec.get_progress(site_id, run_id)
    wtype = prog.get("workflow_type") or run_exec.detect_workflow_type(site_id)
    if wtype == "download":
        return _read_download_output(site_id, run_id, prog)

    out_path = prog.get("output_path")
    if not out_path or not Path(out_path).exists():
        return {
            "error": "output.json not found",
            "output_path": out_path,
            "state": prog.get("state"),
        }
    records = _load_records(Path(out_path))
    if records is None:
        return {"error": "output is not a record list", "output_path": out_path}
    dicts = [r for r in records if isinstance(r, dict)]
    field_union = sorted(set().union(*(r.keys() for r in dicts))) if dicts else []
    schema_consistent = (len({frozenset(r.keys()) for r in dicts}) <= 1) if dicts else True
    return {
        "record_count": len(records),
        "field_union": field_union,
        "schema_consistent": schema_consistent,
        "sample_records": dicts[:3],
        "last_status": prog.get("last_status"),
        "output_path": out_path,
    }


def _read_download_output(site_id: str, run_id: str, prog: dict) -> dict:
    from mcp_server.schemas import DownloadManifestFile

    ws = _workspace(site_id)
    try:
        manifest = DownloadManifestFile.load(ws / "download_manifest.yaml")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"no/invalid download_manifest.yaml: {type(exc).__name__}: {exc}"}
    work_dir = Path(prog.get("output_path") or (ws / "runs" / run_id))
    out_dir = work_dir / manifest.output_dir
    files = []
    for t in manifest.files:
        fp = out_dir / t.filename
        size = fp.stat().st_size if fp.exists() else 0
        files.append(
            {
                "filename": t.filename,
                "exists": fp.exists(),
                "bytes": size,
                "min_bytes": t.min_bytes,
                "non_empty": fp.exists() and size >= max(0, t.min_bytes),
            }
        )
    present = sum(1 for f in files if f["exists"])
    return {
        "files_declared": len(files),
        "files_present": present,
        "all_present": bool(files) and present == len(files),
        "all_non_empty": bool(files) and all(f["non_empty"] for f in files),
        "files": files,
        "output_dir": str(out_dir),
        "last_status": prog.get("last_status"),
    }


# ── human takeover of the LIVE workflow browser ──────────────────────

_TAKEOVER_TIMEOUT_S = 600.0  # block cap waiting for the user (config-overridable)
_TAKEOVER_POLL_S = 1.0


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


async def request_user_takeover(
    site_id: str,
    run_id: str,
    reason: str,
    *,
    page_url: str | None = None,
    wall_type: str = "login",
    message: str = "",
    timeout_s: float = _TAKEOVER_TIMEOUT_S,
) -> dict:
    """Stream the LIVE workflow browser to the watching user so they can solve a
    login / captcha / challenge wall IN THE CRAWL'S OWN context (the session
    continues authenticated — the workflow's next retry goes through).

    Unlike explore's takeover (which opens a fresh headed login browser), this
    attaches to the chromium the workflow itself launched: the run injects
    WORKFLOW_CDP_PORT and contract-compliant workflows pass it to chromium's
    --remote-debugging-port. Headless is fine — the user sees a CDP screencast.

    Writes the same workspace-root ``_takeover_request.json`` handshake the
    explore phase uses (the backend watcher + WS bridge + frontend window are
    shared), BLOCKS until the user marks it done (or ``timeout_s``), suspending
    the crawl's kill thresholds meanwhile, then best-effort merges the browser's
    storage_state into the project ``auth_state.json`` for future runs."""
    h = run_exec.get_handle(site_id, run_id)
    if h is None or h.state not in ("starting", "running"):
        return {
            "ok": False,
            "reason": "crawl is not running — takeover attaches to the LIVE workflow "
            "browser. If the crawl already died on the wall, append_feedback instead "
            "(the next /explore handles auth via its own takeover).",
            "crawl_state": h.state if h else "unknown",
        }
    port = h.cdp_port
    cdp_http = f"http://127.0.0.1:{port}" if port else None

    # Probe the port: a pre-contract workflow (ignores WORKFLOW_CDP_PORT) or a
    # browserless (pure-httpx) workflow never opens it.
    import httpx

    alive = False
    if cdp_http:
        for _ in range(3):
            try:
                async with httpx.AsyncClient() as c:
                    r = await c.get(f"{cdp_http}/json/version", timeout=2)
                alive = r.status_code == 200
                if alive:
                    break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
    if not alive:
        return {
            "ok": False,
            "reason": "the workflow browser does not expose CDP — either a workflow "
            "from before the WORKFLOW_CDP_PORT contract, or it drives no browser at "
            "all (pure httpx). Takeover unavailable; use inspect_crawl_failure to "
            "diagnose and append_feedback to report.",
        }

    from mcp_server.browser import _discover_page_ws  # lazy: pulls playwright deps

    cdp_ws = await _discover_page_ws(cdp_http, page_url)

    req_path = _workspace(site_id) / "_takeover_request.json"
    payload = {
        "status": "pending",
        "reason": reason,
        "message": message,
        "wall_type": wall_type or "login",
        "page_url": page_url,
        "cdp_http": cdp_http,
        "cdp_ws": cdp_ws,
        "viewport": {"width": 1280, "height": 900},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "phase": "run",
    }
    _atomic_write_json(req_path, payload)
    run_exec.begin_takeover(h)

    waited, done = 0.0, False
    try:
        while waited < max(30.0, timeout_s):
            await asyncio.sleep(_TAKEOVER_POLL_S)
            waited += _TAKEOVER_POLL_S
            if h.state not in ("starting", "running"):
                break  # crawl ended under the takeover — browser is gone; stop waiting
            try:
                cur = json.loads(req_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if cur.get("status") == "done":
                done = True
                break
    finally:
        run_exec.end_takeover(h)
        try:
            req_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not done:
        if h.state not in ("starting", "running"):
            return {
                "ok": False,
                "completed": False,
                "reason": f"crawl reached state={h.state} during the takeover — the "
                "workflow (and its browser) exited before the user finished.",
                "crawl_state": h.state,
            }
        return {
            "ok": False,
            "completed": False,
            "reason": f"takeover timed out after {timeout_s:.0f}s (no user action); "
            "kill thresholds resumed, crawl continues",
            "crawl_state": h.state,
        }

    # User finished inside the crawl's own context — its session is already
    # authenticated. Best-effort: also merge the browser's storage_state into the
    # project-global auth_state.json so FUTURE runs/validations start authed.
    auth_saved = False
    try:
        from playwright.async_api import async_playwright

        from mcp_server.browser import _load_json, _merge_storage_state

        async with async_playwright() as pw:
            br = await pw.chromium.connect_over_cdp(cdp_http)
            try:
                if br.contexts:
                    state = await br.contexts[0].storage_state()
                    auth = resolve_auth_path()
                    auth.parent.mkdir(parents=True, exist_ok=True)
                    merged = _merge_storage_state(_load_json(auth), state)
                    auth.write_text(
                        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    auth_saved = True
            finally:
                await br.close()  # connect_over_cdp: disconnects; browser keeps running
    except Exception:  # noqa: BLE001 — auth save is a bonus, never fail the takeover
        pass

    return {
        "ok": True,
        "completed": True,
        "auth_saved": auth_saved,
        "crawl_state": h.state,
        "hint": "poll_crawl now — lines_emitted resuming growth means the workflow recovered",
    }


# ── optional failure diagnosis (fresh playwright, read-only) ─────────

# Page-level HTML cleaner for the INLINE overview — mirrors validator_browser's
# _CLEAN_RECORD_JS heavy-mode rules: drop style/noscript, collapse big <script>
# bodies (keep the tag visible), collapse svg path geometry + data: URIs +
# overlong attrs. High signal-per-token for judging selector drift / structure.
# (The full RAW page.content() is saved to disk separately for ground truth.)
_CLEAN_PAGE_JS = r"""
() => {
  const root = document.documentElement;
  if (!root) return "";
  const c = root.cloneNode(true);
  c.querySelectorAll('style,noscript').forEach(e => e.remove());
  c.querySelectorAll('script').forEach(e => {
    const t = (e.textContent || '').trim();
    if (t.length > 200) e.textContent = t.slice(0, 200) + '…';
  });
  c.querySelectorAll('path[d]').forEach(p => p.setAttribute('d', '…'));
  c.querySelectorAll('*').forEach(e => {
    for (const a of [...e.attributes]) {
      if (a.value && a.value.startsWith('data:')) e.setAttribute(a.name, 'data:…');
      else if (a.value && a.value.length > 200) e.setAttribute(a.name, a.value.slice(0, 200) + '…');
    }
  });
  return c.outerHTML;
}
"""


async def inspect_crawl_failure(
    site_id: str,
    run_id: str,
    source_url: str,
    selector: str | None = None,
    *,
    full_page: bool = True,
    excerpt_cap: int = 4000,
    settle_ms: int = 2500,
    selector_wait_ms: int = 8000,
) -> dict:
    """Re-fetch one page with a fresh AUTHED anti-bot browser to tell
    **site-down/blocked vs selector-drift** on a 0-record / anti-bot crawl
    result. Saves a full-page screenshot + the complete raw HTML under
    runs/<run_id>/snapshots/ (the agent can ``Read`` either); returns a CLEANED
    inline overview + paths + verdict.

    This is an INDEPENDENT cold fetch (its own browser) — it carries auth +
    anti-bot evasion so it reflects an authed view, but it is NOT the workflow's
    live session/state. It writes ONLY to runs/<run_id>/snapshots/."""
    from playwright.async_api import async_playwright

    from runtime.validator_browser import _antibot_context  # reuse UA+init+auth context

    snap = _run_dir(site_id, run_id) / "snapshots"
    snap.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(source_url.encode("utf-8")).hexdigest()[:8]
    png_path: Path | None = snap / f"inspect_{h}.png"
    html_path: Path | None = snap / f"inspect_{h}.html"
    auth = resolve_auth_path()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=os.environ.get("CRAWLER_EXPLORER_HEADLESS", "1") != "0",
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx, page = await _antibot_context(
            browser, storage_state=str(auth) if auth.exists() else None
        )
        try:
            resp = await page.goto(source_url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else None
            await page.wait_for_timeout(settle_ms)  # let JS render
            if selector:  # give the selector a chance to appear; tolerate timeout
                try:
                    await page.wait_for_selector(selector, timeout=selector_wait_ms)
                except Exception:  # noqa: BLE001
                    pass
            try:  # trigger lazy loads
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
            title = await page.title()
            body_len = await page.evaluate(
                "() => document.body ? document.body.innerText.length : 0"
            )
            match = None
            if selector:
                match = await page.evaluate("(s) => document.querySelectorAll(s).length", selector)
            raw_html = await page.content()
            cleaned = await page.evaluate(_CLEAN_PAGE_JS)
            try:
                await page.screenshot(path=str(png_path), full_page=full_page)
            except Exception:  # noqa: BLE001 — screenshot is best-effort
                png_path = None
            try:
                html_path.write_text(raw_html, encoding="utf-8")
            except OSError:
                html_path = None
        except Exception as exc:  # noqa: BLE001 — nav failure IS the signal
            await browser.close()
            return {
                "source_url": source_url,
                "reachable": False,
                "auth_loaded": auth.exists(),
                "error": f"{type(exc).__name__}: {exc}",
                "verdict": "site_down_or_unreachable",
            }
        await browser.close()

    if (status is not None and status >= 400) or body_len < 200:
        verdict = "site_down_or_blocked"  # 4xx/5xx or near-empty body (challenge/interstitial)
    elif selector is not None and match == 0:
        verdict = "site_ok_selector_drift"  # page renders fine but the selector finds nothing
    else:
        verdict = "site_ok"
    return {
        "source_url": source_url,
        "reachable": True,
        "auth_loaded": auth.exists(),
        "http_status": status,
        "title": title,
        "body_text_len": body_len,
        "selector": selector,
        "selector_match_count": match,
        "verdict": verdict,
        "screenshot_path": str(png_path) if png_path else None,
        "html_path": str(html_path) if html_path else None,
        "html_excerpt": (cleaned or "")[:excerpt_cap],
    }


# ── server builder (config injects the kill thresholds) ──────────────


_DEFAULT_CONFIG = {
    "crawl_mode": "full",
    "wall_clock_cap_s": run_exec.DEFAULT_WALL_CLOCK_CAP_S,
    "stall_timeout_s": run_exec.DEFAULT_STALL_TIMEOUT_S,
    "python_exe": None,
    "output_root": None,
    "takeover_timeout_s": _TAKEOVER_TIMEOUT_S,
}


def build_runner_server(config: dict | None = None):
    """In-process SDK MCP server exposing the Run agent's entire tool surface.
    ``config`` carries the deterministic kill thresholds (not agent-owned)."""
    cfg = {**_DEFAULT_CONFIG, **(config or {})}

    @tool(
        "start_crawl",
        "Launch the validated workflow.py as a FULL crawl (CRAWL_MODE=full) in an isolated "
        "runs/<run_id>/ workdir, streaming in the background. Returns immediately — then call "
        "poll_crawl repeatedly until the state is terminal. After a TERMINAL attempt "
        "(done/killed/error) it may be called AGAIN with the same run_id to RETRY (same "
        "workdir; the log appends across attempts) — see the retry discipline: diagnose "
        "first, one retry max, only on transient evidence.",
        {"site_id": str, "run_id": str},
    )
    @_safe
    async def _t_start(args):
        return _ok(
            await run_exec.start_crawl(
                args["site_id"],
                args["run_id"],
                crawl_mode=cfg["crawl_mode"],
                wall_clock_cap_s=cfg["wall_clock_cap_s"],
                stall_timeout_s=cfg["stall_timeout_s"],
                python_exe=cfg["python_exe"],
                output_root=cfg["output_root"],
            )
        )

    @tool(
        "poll_crawl",
        "Read the crawl's live signals: state (running/done/killed/error), elapsed_s, "
        "lines_emitted, last_output_age_s, stdout_tail, exit_code, killed_reason, record_count. "
        "Pass wait_s (e.g. 90-300) to block server-side; the call returns EARLY (wake_reason="
        "'alert', details in new_alerts) on a stall warning (silence past half the stall "
        "timeout), a Python traceback, or a stdout line matching wake_pattern — a regex you "
        "should set to this site's failure markers (e.g. '(?i)captcha|login|403'); it persists "
        "across polls. So prefer LONG wait_s — anomalies wake you anyway. Call repeatedly until "
        "state is terminal.",
        _schema(
            {"site_id": str, "run_id": str, "wait_s": int, "wake_pattern": str},
            ["site_id", "run_id"],
        ),
    )
    @_safe
    async def _t_poll(args):
        w = args.get("wait_s")
        return _ok(
            await poll_with_wait(
                args["site_id"],
                args["run_id"],
                float(w) if isinstance(w, (int, float)) else 0.0,
                wake_pattern=args.get("wake_pattern") or None,
            )
        )

    @tool(
        "kill_crawl",
        "Deliberately TERMINATE a running crawl you have diagnosed as doomed — systematic "
        "failure pattern (every request captcha/403-blocked, zero new records, no recovery "
        "sign) or budget math provably unreachable with ~zero yield. Diagnose FIRST "
        "(stdout_tail, inspect_crawl_failure); never kill for slowness alone — healthy-yield "
        "slowness should run to the wall clock (partial data gates PARTIAL). Blocked while a "
        "human takeover is active. The kill still post-processes: an incremental-flush "
        "workflow leaves partial output (check read_crawl_output after). Then record "
        "within_budget=fail with basis 'agent kill: <reason>'.",
        {"site_id": str, "run_id": str, "reason": str},
    )
    @_safe
    async def _t_kill(args):
        return _ok(
            await run_exec.kill_crawl(args["site_id"], args["run_id"], args.get("reason") or "")
        )

    @tool(
        "read_crawl_output",
        "Parse the kept deliverable (extraction/api: output.json record_count/fields/sample; "
        "download: declared files present/non-empty). Call after the crawl is terminal.",
        {"site_id": str, "run_id": str},
    )
    @_safe
    async def _t_read_out(args):
        return _ok(read_crawl_output(args["site_id"], args["run_id"]))

    @tool(
        "inspect_crawl_failure",
        "Diagnose a 0-record / anti-bot crawl: re-fetch source_url with a fresh AUTHED anti-bot "
        "browser to tell site-down/blocked vs selector-drift. Saves a full-page screenshot + the "
        "complete raw HTML to runs/<run_id>/snapshots/ (use Read on screenshot_path/html_path for "
        "deeper diagnosis) and returns a CLEANED inline overview + verdict. selector optional. "
        "Independent cold fetch (carries auth, but NOT the workflow's live state). Use to decide "
        "whether a problem is worth feeding back to /explore.",
        _schema(
            {"site_id": str, "run_id": str, "source_url": str, "selector": str},
            ["site_id", "run_id", "source_url"],
        ),
    )
    @_safe
    async def _t_inspect(args):
        return _ok(
            await inspect_crawl_failure(
                args["site_id"], args["run_id"], args["source_url"], args.get("selector") or None
            )
        )

    @tool(
        "init_run_manifest",
        "Create a fresh run manifest at start. workflow_type: 'extraction' (default), 'download', "
        "or 'api' (records like extraction). crawl_mode defaults 'full'.",
        _schema(
            {"site_id": str, "run_id": str, "workflow_type": str, "crawl_mode": str},
            ["site_id", "run_id"],
        ),
    )
    @_safe
    async def _t_init(args):
        return _ok(
            init_run_manifest(
                args["site_id"],
                args["run_id"],
                args.get("workflow_type") or "extraction",
                args.get("crawl_mode") or "full",
            )
        )

    @tool(
        "update_run_manifest",
        "Set one completion dimension's status (pass/fail/not_verified/advisory/n/a) + basis + "
        "evidence; returns the recomputed outcome. Pass record_count + output_path when recording "
        "non_trivial. basis/evidence/record_count/output_path optional.",
        _schema(
            {
                "site_id": str,
                "run_id": str,
                "dim": str,
                "status": str,
                "basis": str,
                "evidence": str,
                "record_count": int,
                "output_path": str,
            },
            ["site_id", "run_id", "dim", "status"],
        ),
    )
    @_safe
    async def _t_update(args):
        rc = args.get("record_count")
        return _ok(
            update_run_manifest(
                args["site_id"],
                args["run_id"],
                args["dim"],
                args["status"],
                basis=args.get("basis") or "",
                evidence=args.get("evidence") or "",
                record_count=rc if isinstance(rc, int) else None,
                output_path=args.get("output_path") or "",
            )
        )

    @tool(
        "read_run_manifest",
        "Read current run manifest state + computed outcome.",
        {"site_id": str, "run_id": str},
    )
    @_safe
    async def _t_read_man(args):
        return _ok(read_run_manifest(args["site_id"], args["run_id"]))

    @tool(
        "write_run_report",
        "Append a narrative section to runs/<run_id>/report.md.",
        {"site_id": str, "run_id": str, "section": str},
    )
    @_safe
    async def _t_report(args):
        return _ok(write_run_report(args["site_id"], args["run_id"], args["section"]))

    @tool(
        "append_feedback",
        "Append a hypothesis-feedback item to runs/<run_id>/feedback.yaml (NOT explore's "
        "hypotheses.yaml). The next /explore reads it. Use for problems the FULL crawl revealed "
        "that sample-validation couldn't (scale anti-bot, deep selector drift, low yield). "
        "basis/priority/notes optional.",
        _schema(
            {
                "site_id": str,
                "run_id": str,
                "claim": str,
                "basis": str,
                "priority": str,
                "notes": str,
            },
            ["site_id", "run_id", "claim"],
        ),
    )
    @_safe
    async def _t_feedback(args):
        return _ok(
            append_feedback(
                args["site_id"],
                args["run_id"],
                args["claim"],
                basis=args.get("basis") or "",
                priority=args.get("priority") or "medium",
                notes=args.get("notes") or "",
            )
        )

    @tool(
        "request_user_takeover",
        "Stream the LIVE crawl browser to the watching user so they solve a login / captcha / "
        "challenge wall IN the workflow's own session (it continues authenticated afterwards). "
        "Use when the crawl is STILL RUNNING but stuck on a wall (stdout_tail login/captcha "
        "hints, output stalled, inspect_crawl_failure says blocked). reason = short internal "
        "label; message = user-facing markdown IN THE USER'S LANGUAGE (what happened + what to "
        "do); wall_type = login|login_modal|captcha|challenge; page_url = the wall URL if known. "
        "BLOCKS up to ~10 min while the user works (crawl kill thresholds are paused). May "
        "report takeover unavailable (pre-contract or browserless workflow) — then use "
        "append_feedback instead. If the crawl already DIED on the wall, don't call this.",
        _schema(
            {
                "site_id": str,
                "run_id": str,
                "reason": str,
                "page_url": str,
                "wall_type": str,
                "message": str,
            },
            ["site_id", "run_id", "reason"],
        ),
    )
    @_safe
    async def _t_takeover(args):
        return _ok(
            await request_user_takeover(
                args["site_id"],
                args["run_id"],
                args["reason"],
                page_url=args.get("page_url") or None,
                wall_type=args.get("wall_type") or "login",
                message=args.get("message") or "",
                timeout_s=float(cfg.get("takeover_timeout_s") or _TAKEOVER_TIMEOUT_S),
            )
        )

    @tool(
        "send_user_message",
        "Send a short message directly to the USER watching this run — it appears as a chat "
        "bubble in their conversation. Use it to TALK to the person: acknowledge + answer their "
        "mid-run guidance, flag something needing attention, or report a notable finding. Write "
        "in the user's language. One-way and non-pausing; your plain assistant text is NOT shown "
        "to them as chat, so this tool is the ONLY way to reach their conversation. Keep it brief "
        "— not a log, no data dumps.",
        {"site_id": str, "run_id": str, "message": str},
    )
    @_safe
    async def _t_send_msg(args):
        return _ok(send_user_message(args["site_id"], args["run_id"], args["message"]))

    tools = [
        _t_start,
        _t_poll,
        _t_kill,
        _t_read_out,
        _t_inspect,
        _t_init,
        _t_update,
        _t_read_man,
        _t_report,
        _t_feedback,
        _t_takeover,
        _t_send_msg,
    ]
    return create_sdk_mcp_server("runner-tools", version="0.1.0", tools=tools)


RUNNER_TOOL_NAMES = [
    "start_crawl",
    "poll_crawl",
    "kill_crawl",
    "read_crawl_output",
    "inspect_crawl_failure",
    "init_run_manifest",
    "update_run_manifest",
    "read_run_manifest",
    "write_run_report",
    "append_feedback",
    "request_user_takeover",
    "send_user_message",
]


__all__ = ["build_runner_server", "RUNNER_TOOL_NAMES"]
