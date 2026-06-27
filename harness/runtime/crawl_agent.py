"""Agentic Crawl agent — the harness's second terminal route.

Where Explore writes a DETERMINISTIC workflow.py (run later by the Run agent),
this agent IS the crawl: a persistent SDK session that drives the browser, writes
batch-extraction code, and commits records as it goes until it judges the harvest
complete against the crawl_brief's completeness anchor. Explore picks this route
(over validate→run) when a site's control flow is too dynamic for static code.

It is the THIRD agent archetype — Explore's full browser surface × Run's
kept-data semantics × the Data Agent's "run autonomously until done":

  - Capability surface: the project's browser-harness MCP (browser_* drive +
    walls) + a narrow in-process ``crawl`` emit server (commit/cursor/finalize)
    + Read. NO Bash/Write/Edit — extraction code runs in-memory via browser_player
    and output flows ONLY through commit_records (isolation by tool surface, like
    the Run agent, but with the browser instead of the runner tools).
  - Output: ``runs/<run_id>/records.jsonl`` (truth) → ``output.json`` on finalize.
  - Supervision: the deterministic core here enforces a PROGRESS-STALL kill (no
    new commits / cursor movement) as the primary brake — more precise than a
    token cap for an open-ended loop — plus a wall-clock cap. (Per the operator's
    decision, there is NO token budget cap.)

The session's final line is the same outcome contract the Run agent uses, parsed
by ``runtime.cli``:

    [COMPLETE|PARTIAL|FAILED|ABORTED] <site_id> run_id=<run-XXXX> records=<N> — <reason>
"""

from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import AssistantMessage, ResultMessage, query

from runtime import crawl_emit, modality
from runtime.crawl_emit import EMIT_TOOL_NAMES, build_emit_server
from runtime.options import INSTRUCTION_PRECEDENCE, assert_api_key, build_options
from runtime.run_logger import RunLogger, jsonable_usage
from runtime.run_agent import (  # reuse the run agent's event/summary/parse machinery verbatim
    OUTCOME_LINE_RE,
    RunAgentSummary,
    _msg_events,
    _parse_outcome_line,
)
from mcp_server.schemas import RunManifestFile


# ── supervision defaults (NO token cap — operator decision) ──────────
DEFAULT_WALL_CLOCK_CAP_S = 7200.0  # 2h — agentic loops run long
DEFAULT_STALL_WARN_S = 300.0  # 5m no progress → soft steering warning
DEFAULT_STALL_KILL_S = 900.0  # 15m no progress → abort (generous: walls/takeovers)
_SUPERVISE_POLL_S = 20.0


# Browser-harness tools the agent may DRIVE with (the workspace_* writers that
# build explore artifacts are withheld — this agent produces DATA, not code).
_BROWSER_DRIVE_TOOLS = [
    "browser_attach",
    "browser_goto",
    "browser_evaluate",
    "browser_content",
    "browser_cdp_send",
    "browser_player",
    "browser_snapshot",
    "browser_check_login_wall",
    "browser_request_user_login",
    "browser_save_auth",
    "workspace_attach",
    "workspace_read",
    "workspace_list_samples",
]


AGENTIC_CRAWL_ROLE = """\
## Agentic Crawl agent
You are a persistent crawl agent. The Explore phase decided this site CANNOT be
scraped by a single deterministic workflow.py — its control flow is too dynamic —
so you harvest the data YOURSELF: drive the browser, write extraction code, react
to what each page actually shows, and keep going until the data is complete
against the crawl_brief's completeness anchor.

READ workspaces/<site>/crawl_brief.md FIRST (workspace_read "crawl_brief.md"). It
carries: the goal + field schema; the COMPLETENESS ANCHOR (your done-judgment
basis, marked hard|soft); why this is agentic (the dynamics to handle); proven
knowledge (helpers / selectors / endpoints / auth); hazards (walls, safe cadence);
and a recommended strategy.

Core discipline:
- WRITE CODE, RUN BATCHES. Your main path is browser_player (async Python with
  page/context/cdp in scope): write an extraction routine, run it over a BATCH,
  commit_records the results. Cost scales with the number of DISTINCT page shapes
  you handle, NOT the record count — one routine per shape, not one LLM read per
  row. Hand-scrape single units only for small high-value exceptions.
- COMMIT AS YOU GO. Never hold the only copy of scraped data in memory:
  commit_records every batch (the truth file survives a kill) and mark_cursor
  each time you advance. You resume from the cursor.
- ADAPT. Page shape changed? Rewrite the routine — that is the whole point of
  this route. A login/captcha wall? You hold the browser: browser_check_login_wall
  + browser_request_user_login to have the user clear it, then continue.
- ACCOUNT FOR EVERY UNIT. record_skip (with a reason) any unit you finally skip,
  so completeness can say "N scraped + M skipped = full set accounted for".
- JUDGE DONE AGAINST THE ANCHOR. get_crawl_progress; for a HARD anchor, finish
  when the cursor reaches the full set and every unit is committed or skipped;
  for a SOFT anchor, when your stop heuristic is met — and justify it.
- PROGRESS STALL IS KILLED. The driver kills a crawl that stops advancing (no new
  commits or cursor movement for a long stretch). Keep progress visible; if a wall
  is unpassable, record_skip and move on, or finalize — don't spin in place.

A user may WATCH and message you mid-crawl; their messages arrive as user turns.
Your plain text is NOT shown to them — reply with send_user_message FIRST
(acknowledge + answer / say how you're adjusting), then continue.

When done: finalize_crawl(completeness_status, completeness_basis citing the
anchor) returns the gate-computed outcome. Then emit, as your LAST line:

    [COMPLETE|PARTIAL|FAILED|ABORTED] <site_id> run_id=<run-XXXX> records=<N> — <one-line reason>

It MUST equal read_run_manifest's outcome (ERROR only if a tool itself crashes).
"""


def _allowed_tools(model: str | None = None) -> list[str]:
    # The agentic crawl agent gates by ALLOW-list, so it INCLUDES exactly this
    # base-model mode's vision tool: browser_screenshot for a multimodal model,
    # browser_vision_probe for a text model. See runtime/modality.py.
    drive = list(_BROWSER_DRIVE_TOOLS)
    drive += [t for t in modality.enabled_vision_tools(model) if t not in drive]
    return (
        ["Read"]
        + [f"mcp__crawl__{t}" for t in EMIT_TOOL_NAMES]
        + [f"mcp__browser-harness__{t}" for t in drive]
    )


def _build_prompt(site_id: str, run_id: str, brief_exists: bool) -> str:
    brief_line = (
        'Start by reading workspaces/%s/crawl_brief.md (workspace_read "crawl_brief.md").' % site_id
        if brief_exists
        else "NOTE: no crawl_brief.md found — fall back to goal.md / selectors.yaml / helpers.py "
        "and infer the anchor yourself (mark it soft)."
    )
    return (
        f"Harvest the data for site '{site_id}' via the agentic route. run_id = {run_id}.\n\n"
        f"{brief_line}\n\n"
        f"Then: init_crawl('{site_id}', '{run_id}', identifier_field=<from the brief>, "
        f"anchor=<the brief's completeness_anchor, incl. hardness + estimated_total if hard>); "
        f"drive the browser + write batch-extraction code (browser_player); commit_records as you "
        f"go (mark_cursor each advance); record_skip blockers with a reason; and finalize_crawl "
        f"when you judge it complete against the anchor.\n\n"
        f"Your LAST line MUST be the outcome contract:\n\n"
        f"    [COMPLETE|PARTIAL|FAILED|ABORTED] {site_id} run_id={run_id} records=<N> — <one-line reason>\n\n"
        f"equal to read_run_manifest's outcome."
    )


def _build_options(
    root: Path, *, permission_mode: str, max_turns: int, model: str | None, config: dict
):
    """Full browser surface (like Explore) + the in-process crawl emit server,
    narrowed to a browser-drive + emit + Read allow-list (no Bash/Write/Edit).
    The agentic crawl role rides the claude_code preset at SYSTEM authority."""
    br = build_options(
        root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        allowed_tools=_allowed_tools(model),
        extra_mcp_servers={"crawl": build_emit_server(config)},
        # Forward the RESOLVED base-model mode (+ submodel backend for a text run)
        # so the MCP server's vision backstops agree with this allow-list gate.
        extra_env=modality.vision_env(model),
        append_system_prompt=INSTRUCTION_PRECEDENCE + "\n\n" + AGENTIC_CRAWL_ROLE,
    )
    return br.options


async def crawl_agent(
    site_id: str,
    *,
    run_id: str | None = None,
    project_root: Path | str | None = None,
    permission_mode: str = "bypassPermissions",
    max_turns: int = 200,
    model: str | None = None,
    wall_clock_cap_s: float = DEFAULT_WALL_CLOCK_CAP_S,
    stall_warn_s: float = DEFAULT_STALL_WARN_S,
    stall_kill_s: float = DEFAULT_STALL_KILL_S,
    steer_poll_s: float = 1.0,
    steer_offset: int | None = None,
    summary: RunAgentSummary | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Drive a self-contained Agentic Crawl SDK session. Yields normalized event
    dicts; updates ``summary`` in place (outcome/run_id/record_count/reason from
    the final outcome line, falling back to the manifest's computed outcome)."""
    if summary is None:
        summary = RunAgentSummary()
    summary.site_id = site_id
    summary.workflow_type = "agentic"
    summary.crawl_mode = "agentic"
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    summary.run_id = run_id

    assert_api_key()
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent
    brief_path = crawl_emit._workspace(site_id) / "crawl_brief.md"
    options = _build_options(
        root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        config={},
    )
    prompt = _build_prompt(site_id, run_id, brief_path.exists())

    rl = RunLogger(
        role="crawl",
        site_id=site_id,
        config={
            "model": model,
            "max_turns": max_turns,
            "run_id": run_id,
            "workflow_type": "agentic",
            "wall_clock_cap_s": wall_clock_cap_s,
            "stall_warn_s": stall_warn_s,
            "stall_kill_s": stall_kill_s,
            "permission_mode": permission_mode,
        },
    )
    summary.run_log_path = str(rl.path)

    # ── live mid-crawl steering (mirrors run_agent) ──────────────────────
    steer_queue: asyncio.Queue = asyncio.Queue()
    steer_path = crawl_emit._workspace(site_id) / "user_run_steering.md"
    _input_closed = False

    def _close_input() -> None:
        nonlocal _input_closed
        if not _input_closed:
            _input_closed = True
            try:
                steer_queue.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass

    async def _input_stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt},
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

    if steer_offset is not None:
        _steer_offset = max(0, int(steer_offset))
    else:
        try:
            _steer_offset = steer_path.stat().st_size if steer_path.is_file() else 0
        except OSError:
            _steer_offset = 0

    async def _tail_steering() -> None:
        nonlocal _steer_offset
        while True:
            await asyncio.sleep(steer_poll_s)
            try:
                if not steer_path.is_file() or steer_path.stat().st_size <= _steer_offset:
                    continue
                with steer_path.open("r", encoding="utf-8") as f:
                    f.seek(_steer_offset)
                    new = f.read()
                    _steer_offset = f.tell()
            except asyncio.CancelledError:
                raise
            except OSError:
                continue
            body = "\n".join(
                ln for ln in new.splitlines() if not ln.strip().startswith("## Operator steering @")
            ).strip()
            if body:
                steer_queue.put_nowait(
                    "⚑ OPERATOR MESSAGE (live, mid-crawl) — the user watching this crawl is "
                    f"talking to you. Reply FIRST with send_user_message('{site_id}', '{run_id}', "
                    "...) — acknowledge + answer / say how you're adjusting; a bare \"OK\" doesn't "
                    "count; fold multiple messages into one reply. It does NOT pause the crawl; "
                    "reply, adapt, then continue.\n\n" + body
                )

    # ── progress-stall + wall-clock supervision (the primary brake) ──────
    abort_event = asyncio.Event()
    abort_reason: str | None = None
    started = time.monotonic()

    async def _supervise() -> None:
        nonlocal abort_reason
        last_processed, last_cursor, last_progress_t, warned = -1, object(), started, False
        while not abort_event.is_set():
            await asyncio.sleep(_SUPERVISE_POLL_S)
            now = time.monotonic()
            if now - started > wall_clock_cap_s:
                abort_reason = f"wall_clock_cap exceeded ({wall_clock_cap_s:.0f}s)"
                abort_event.set()
                return
            prog = crawl_emit.get_crawl_progress(site_id, run_id)
            if not isinstance(prog, dict) or prog.get("error"):
                continue  # not init_crawl'd yet — no progress clock running
            processed = prog.get("processed") or 0
            cursor = prog.get("cursor")
            if processed > last_processed or cursor != last_cursor:
                last_processed, last_cursor, last_progress_t, warned = processed, cursor, now, False
                continue
            idle = now - last_progress_t
            if idle > stall_kill_s:
                abort_reason = (
                    f"progress_stall: no new commits or cursor advance for {idle:.0f}s "
                    f"(committed={processed})"
                )
                abort_event.set()
                return
            if idle > stall_warn_s and not warned:
                warned = True
                steer_queue.put_nowait(
                    f"⚑ PROGRESS STALL WARNING — no new commits or cursor movement for {idle:.0f}s "
                    f"(hard kill at {stall_kill_s:.0f}s). If you're stuck: change strategy, "
                    "record_skip the blocker and move on, or finalize_crawl with what you have. "
                    "If you're mid-takeover or legitimately working a hard unit, keep going — but "
                    "commit/mark_cursor as soon as you can so the clock resets."
                )

    steer_task = asyncio.create_task(_tail_steering())
    supervise_task = asyncio.create_task(_supervise())
    abort_wait = asyncio.ensure_future(abort_event.wait())

    chunks: list[str] = []
    aborted = False
    agen = None
    try:
        agen = query(prompt=_input_stream(), options=options).__aiter__()
        while True:
            nxt = asyncio.ensure_future(agen.__anext__())
            done, _ = await asyncio.wait({nxt, abort_wait}, return_when=asyncio.FIRST_COMPLETED)
            if abort_wait in done:
                nxt.cancel()
                aborted = True
                break
            try:
                msg = nxt.result()
            except StopAsyncIteration:
                break
            if isinstance(msg, AssistantMessage):
                rl.write(
                    {
                        "event": "agent_turn",
                        "turn": summary.turn_count + 1,
                        "model": getattr(msg, "model", None),
                        "usage": jsonable_usage(getattr(msg, "usage", None)),
                    }
                )
            for ev in _msg_events(msg, summary):
                if ev["kind"] == "assistant_text":
                    chunks.append(ev["text"])
                rl.event(ev)
                yield ev
            if isinstance(msg, ResultMessage):
                _close_input()
        if not aborted:
            rl.write({"event": "run_complete", "summary": summary.to_dict()})
    except Exception as exc:  # noqa: BLE001
        summary.error = f"{type(exc).__name__}: {exc}"
        rl.write(
            {
                "event": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        yield {"kind": "error", "error": summary.error}
    finally:
        _close_input()
        for t in (steer_task, supervise_task, abort_wait):
            t.cancel()
        for t in (steer_task, supervise_task, abort_wait):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if agen is not None:
            try:
                await agen.aclose()  # close the SDK transport even on an abort break
            except Exception:  # noqa: BLE001
                pass
        if aborted:
            # Driver kill: salvage committed data + record within_budget=fail.
            try:
                res = crawl_emit.abort_crawl(site_id, run_id, abort_reason or "killed")
                rl.write({"event": "aborted", "reason": abort_reason, "result": res})
                yield {"kind": "aborted", "reason": abort_reason, "result": res}
            except Exception as exc:  # noqa: BLE001
                summary.error = (summary.error or "") + f" | abort_crawl: {exc}"

    # ── resolve outcome (mirrors run_agent's tail) ───────────────────────
    combined = "\n".join(chunks).rstrip()
    parsed = _parse_outcome_line(combined)
    if parsed is not None and not aborted:
        outcome, _site, parsed_run, rc, reason = parsed
        summary.outcome = outcome
        summary.outcome_line = next(
            (ln for ln in reversed(combined.splitlines()) if OUTCOME_LINE_RE.search(ln)), None
        )
        summary.run_id = parsed_run or run_id
        summary.record_count = rc
        summary.reason = reason
        return

    # No usable outcome line (or aborted): fall back to the manifest's computed
    # outcome — it's authoritative; the line is only a restatement.
    try:
        m = RunManifestFile.load(crawl_emit._workspace(site_id) / "runs" / run_id / "manifest.yaml")
        mo = (m.outcome or "").upper()
        if mo in ("COMPLETE", "PARTIAL", "FAILED", "ABORTED"):
            summary.outcome = mo
            if summary.record_count is None:
                summary.record_count = m.record_count
            summary.reason = abort_reason or summary.reason or "outcome from manifest"
            return
    except Exception:  # noqa: BLE001
        pass
    summary.outcome = "ERROR" if summary.error else "ABORTED"
    summary.reason = abort_reason or summary.error or "no outcome line emitted"


__all__ = [
    "crawl_agent",
    "DEFAULT_WALL_CLOCK_CAP_S",
    "DEFAULT_STALL_KILL_S",
    "DEFAULT_STALL_WARN_S",
]
