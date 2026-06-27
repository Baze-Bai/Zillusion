"""Self-contained Run agent driven by the Claude Agent SDK.

The production terminal stage of the pipeline, mirroring ``runtime.validate``:
a fresh SDK session whose ENTIRE capability surface is the in-process runner MCP
server (``runtime.run_tools.build_runner_server``) plus read-only ``Read``. No
general Bash/Write/Edit and no ``setting_sources`` — so the Run agent physically
cannot modify an explore artifact, and doesn't load the harness skills/hooks.
Isolation is by tool surface, exactly like the validator.

Where the validator runs ``workflow.py`` once in a throwaway dir to CHECK it,
the Run agent runs it FOR REAL at full scope (``CRAWL_MODE=full``), KEEPS the
output under ``runs/<run_id>/``, monitors the (possibly long) crawl via
streamed signals, and feeds problems back to the next ``/explore`` through
``runs/<run_id>/feedback.yaml`` (surfaced by the ``inject_run_feedback``
SessionStart hook).

The agent records each completion dimension in ``runs/<run_id>/manifest.yaml``;
the outcome is GATE-COMPUTED from it (see ``mcp_server.schemas.RunManifestFile``).
The session's final line must be the outcome contract:

    [COMPLETE|PARTIAL|FAILED|ABORTED] <site_id> run_id=<run-XXXX> records=<N> — <reason>

which equals the manifest's computed ``outcome`` (ERROR is the only system-layer
exception). ``runtime.cli`` parses this line.
"""

from __future__ import annotations

import asyncio
import re
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from runtime import pricing, run_exec
from runtime.options import assert_api_key
from runtime.run_logger import RunLogger, jsonable_usage
from runtime.run_tools import RUNNER_TOOL_NAMES, build_runner_server
from mcp_server.schemas import new_run_manifest


# Brackets optional: a model once emitted the line verbatim minus the literal
# [] (deepseek, 2026-06-09) — the `<site> run_id=` anchor right after keeps the
# bare-word form unambiguous, so accept both.
OUTCOME_LINE_RE = re.compile(
    r"\[?(COMPLETE|PARTIAL|FAILED|ABORTED)\]?\s+(\S+)\s+run_id=(\S+)"
    r"(?:\s+records=(\d+))?\s*(?:[—-]\s*(.*))?",
    re.IGNORECASE,
)


def _dimension_help(workflow_type: str) -> tuple[str, str]:
    """(dimension-recording lines, gating-names line) for the detected type —
    derived from the manifest catalog so the prompt names the EXACT dims the
    agent must fill (a wrong dim name -> update_run_manifest KeyError)."""
    m = new_run_manifest("x", "y", workflow_type)
    gating = [n for n, d in m.dimensions.items() if d.gating]
    desc = {
        "launched": "the subprocess spawned",
        "ran_clean": "exit_code 0 and no traceback in the streamed log",
        "produced_output": "output.json exists, parses, is a non-empty record list",
        "non_trivial": "record_count > 0 (note any full-vs-sample delta in evidence)",
        "files_present": "every declared download_manifest file exists + is non-empty",
        "within_budget": "the crawl was NOT killed — neither by the wall-clock / stall "
        "threshold nor by your own kill_crawl (an agent kill records fail here, "
        "basis 'agent kill: <reason>')",
        "partial_failures": "advisory — per-record/file failure rate in the output",
        "full_mode_effective": "advisory — did CRAWL_MODE=full take effect (legacy workflows ignore it)",
    }
    lines = "\n".join(f"   - {n}: {desc.get(n, '')}" for n in m.dimensions)
    return lines, ", ".join(gating)


def _build_system_prompt(workflow_type: str) -> str:
    dim_lines, gating = _dimension_help(workflow_type)
    out_dim = "files_present" if workflow_type == "download" else "produced_output / non_trivial"
    return f"""\
You are an autonomous Run agent. You execute a VALIDATED web-crawler workflow.py
at FULL scope to produce and KEEP the real dataset, monitor the (possibly long)
crawl, and feed genuine problems back to the next /explore. The workflow ALREADY
PASSED validation on a sample — your job is COMPLETION, not re-judging quality.

## Hard isolation
READ-ONLY on every explore artifact (workflow.py / helpers.py / selectors.yaml /
output_sample.json / goal.md / ...). Your tools write ONLY runs/<run_id>/. You
have no general Bash/Write/Edit. The crawl runs in an isolated runs/<run_id>/
workdir; its output never overwrites explore's output_sample.json.

## Your tools (all mcp__runner__*)
Lifecycle: start_crawl (launch the full crawl, returns immediately),
poll_crawl(wait_s=..., wake_pattern=...) (CHECKPOINT — blocks up to wait_s but
wakes EARLY on alerts, returns live signals), kill_crawl (deliberately abort a
doomed crawl — see Kill discipline),
read_crawl_output (parse the kept deliverable), inspect_crawl_failure (re-fetch a
page with a fresh AUTHED anti-bot browser to tell site-down/blocked vs
selector-drift; it saves a full-page screenshot + the complete raw HTML to
runs/<run_id>/snapshots/ — **`Read` screenshot_path / html_path for deeper
diagnosis** — and returns a cleaned inline overview; it carries auth but is an
independent cold fetch, NOT the workflow's live state). Record: init_run_manifest,
update_run_manifest, read_run_manifest, write_run_report, append_feedback.
Talk: send_user_message (a chat bubble in the watching user's conversation).
Walls: request_user_takeover (stream the LIVE crawl browser to the user so they
clear a login/captcha/challenge wall mid-crawl — see below).
Plus read-only Read.

## Login / captcha walls (human takeover)
If the crawl is STILL RUNNING but stuck on an auth wall — stdout_tail shows
login redirects / 401-403 loops / captcha mentions, output stalls while
state=running, or inspect_crawl_failure says blocked or its HTML shows a login
form — call request_user_takeover(site_id, run_id, reason, page_url=<wall URL
if known>, wall_type=login|captcha|challenge, message=<markdown FOR THE USER,
in their language: what the crawl hit + what to do in the streamed window>).
It attaches the user to the workflow's OWN browser (same session — after they
log in, the workflow's next retry proceeds authenticated) and BLOCKS up to
~10 min while they work; the kill thresholds are paused meanwhile. Then
poll_crawl: lines_emitted resuming growth = recovered; send a brief
send_user_message wrap-up. Constraints: if the crawl already DIED on the wall,
do NOT call this (no live browser) — append_feedback instead; the tool may
also report takeover unavailable (a pre-contract or browserless workflow) —
then inspect_crawl_failure + append_feedback is the path. api workflows drive
no browser by design: takeover reporting unavailable is EXPECTED there, and a
persistent 401/403 means the user's key is missing/expired — append_feedback
+ send_user_message, not a takeover. Request a takeover when the evidence
says a HUMAN can fix it; don't bother the user for walls that aren't there.

## Kill discipline (deliberate abort)
A crawl that is ALIVE but DOOMED does not deserve its full wall clock — and a
retry-noise zombie keeps the stall timer fed forever (liveness is not progress;
only YOU can see "0 new records"). kill_crawl(site_id, run_id, reason) when the
evidence is structural, after diagnosis:
- SYSTEMATIC failure: ≥4-5 consecutive systematic failures (captcha / 403 /
  empty responses) with ZERO new records and no recovery sign across ≥2 polls
  covering the failing phase;
- BUDGET math: the remaining work is provably unreachable within the wall
  clock AND yield is ~zero. If yield is healthy, do NOT kill — let the
  wall-clock cap take it (partial data gates PARTIAL);
- CONFIRMED hard wall: inspect_crawl_failure says blocked AND a takeover is
  unavailable / declined / didn't recover the crawl.
Never kill for slowness alone, a single bad batch, or on your first poll of a
phase. Before killing, send_user_message (tell the watching user what you found
and that you're aborting — in their language). After killing: read_crawl_output
(an incremental-flush workflow leaves partial output), record within_budget=fail
with basis 'agent kill: <reason>' plus the other dims honestly, and
append_feedback with the evidence. The gate then yields PARTIAL (partial output
kept) or ABORTED (nothing kept) — same as a threshold kill.

## Retry discipline (one retry max)
A failed crawl (state killed/error, non-zero exit_code, or zero/empty output)
is not automatically final — but never retry blind. Diagnose FIRST: stdout_tail
(traceback? connection/timeout error? anti-bot?) and, when the cause is
unclear, inspect_crawl_failure on the source URL. Then:
- TRANSIENT evidence — a network/connection/timeout error in the log, a stall
  kill, or "the crawl died but the site is reachable on inspection" — call
  start_crawl AGAIN with the SAME site_id/run_id (allowed once the previous
  attempt is terminal; same runs/<run_id>/ workdir, the log appends) and
  monitor as usual. ONE retry, no more — two failures is signal, not noise.
- DETERMINISTIC evidence — selector drift (site_ok_selector_drift), a code
  error that would recur identically, a hard wall you couldn't clear — do NOT
  retry; record the dims honestly and append_feedback instead.
Note the retry in the relevant dims' evidence and in the report (how attempt 1
failed, what attempt 2 did).

## Operator conversation
A user may be WATCHING this run live and can message you mid-crawl (their
messages arrive as user turns, typically while you're between poll_crawl
checkpoints). Your plain assistant text is NOT shown to them as chat — the ONLY
way to reach their conversation is send_user_message. When an operator message
arrives: reply via send_user_message FIRST (acknowledge + answer concretely, in
the user's language), adapt what they asked (e.g. report cadence, what to watch
for, whether to keep going), then continue monitoring. Also use it proactively
for things worth telling a human mid-crawl — a significant anomaly, a major
milestone, or the final wrap-up in plain words. Don't spam it every checkpoint.

## Flow
1. init_run_manifest(site_id, run_id, workflow_type="{workflow_type}").
2. Read inputs/<site_id>/goal.md so you know what the data should look like.
3. start_crawl(site_id, run_id) — runs CRAWL_MODE=full, streamed in the background.
4. CHECKPOINT LOOP: poll_crawl(wait_s=180-300, wake_pattern=<this site's
   failure markers, e.g. "(?i)captcha|login|403">) repeatedly until state is
   terminal (done / killed / error). Long waits are SAFE: the poll returns
   early (wake_reason="alert", details in new_alerts) on a stall warning
   (silence past half the stall timeout), a traceback, or a wake_pattern hit —
   react to the alert (diagnose / takeover / kill_crawl) instead of waiting it
   out. Each checkpoint: watch lines_emitted growth, stdout_tail (anti-bot /
   error hints), last_output_age_s, new_alerts. The deterministic core
   enforces the wall-clock cap + stall timeout for you — a breach ->
   state=killed. A login/captcha wall while still running -> human takeover
   (see the section below). A structurally DOOMED crawl (systematic zero-yield
   failures) -> Kill discipline above. A terminal FAILED attempt
   (killed/error/non-zero exit/no output) -> see Retry discipline below before
   recording dims.
5. read_crawl_output — record_count, fields, schema_consistent, last_status
   (for download: declared files present / non-empty).
6. Record EACH completion dimension with update_run_manifest (the outcome is
   GATE-COMPUTED from them — you do NOT decide it ad-hoc):
{dim_lines}
   Pass record_count + output_path when you record {out_dim}.
7. write_run_report — a short narrative: what ran, elapsed, yield, any anomaly.
8. FEEDBACK (only if warranted): if the FULL crawl revealed a problem
   sample-validation could not — anti-bot escalation after N pages, selector
   drift deep in the list, yield far below the sample rate, a mid-run crash —
   append_feedback(claim, basis, priority). Use inspect_crawl_failure to confirm
   site-down vs selector-drift BEFORE blaming the workflow. A clean COMPLETE
   needs no feedback.
9. read_run_manifest -> take its `outcome` -> emit the outcome line.

## Outcome gate
Gating dims: {gating}. outcome = (within_budget fail -> PARTIAL if output exists
else ABORTED) ; (any other gating fail -> FAILED) ; (all gating pass -> COMPLETE)
; (else PARTIAL). ERROR is ONLY for your own machinery crashing.

## Outcome contract (orchestrator parses via regex — verbatim, your LAST line)

    [COMPLETE|PARTIAL|FAILED|ABORTED] <site_id> run_id=<run-XXXX> records=<N> — <one-line reason>

COMPLETE/PARTIAL/FAILED/ABORTED MUST equal read_run_manifest's `outcome`.

## Discipline
Judge COMPLETION, not correctness — quality was the validator's job. "Fewer
records than I'd like", with no independent basis, is a report note / advisory,
never a FAILED. Hard-record a gating dim only on its stated basis.
"""


@dataclass
class RunAgentSummary:
    """End-of-run aggregate. `outcome` is parsed from the session's final line
    and should equal the manifest's computed `outcome`."""

    site_id: str | None = None
    workflow_type: str = "extraction"
    crawl_mode: str = "full"
    outcome: str = "UNSET"  # COMPLETE | PARTIAL | FAILED | ABORTED | ERROR | UNSET
    outcome_line: str | None = None
    run_id: str | None = None
    record_count: int | None = None
    reason: str | None = None
    turn_count: int = 0
    tool_calls: int = 0
    tool_call_breakdown: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    finished: bool = False
    error: str | None = None
    run_log_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "workflow_type": self.workflow_type,
            "crawl_mode": self.crawl_mode,
            "outcome": self.outcome,
            "outcome_line": self.outcome_line,
            "run_id": self.run_id,
            "record_count": self.record_count,
            "reason": self.reason,
            "turn_count": self.turn_count,
            "tool_calls": self.tool_calls,
            "tool_call_breakdown": dict(self.tool_call_breakdown),
            "total_cost_usd": self.total_cost_usd,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "finished": self.finished,
            "error": self.error,
            "run_log_path": self.run_log_path,
        }


def _write_crash_marker(
    site_id: str, run_id: str, workflow_type: str, crawl_mode: str, error: str
) -> bool:
    """Leave a schema-valid manifest recording that the agent session crashed
    BEFORE init_run_manifest — `launched: fail` gate-computes outcome=failed,
    so the next explore's inject_run_feedback hook surfaces this run (a
    manifest-less run dir would otherwise be invisible to it). No-op when a
    manifest already exists (the crash happened after init — the real one is
    better). Best-effort: returns False instead of raising."""
    try:
        mp = run_exec._workspace(site_id) / "runs" / run_id / "manifest.yaml"
        if mp.exists():
            return False
        mm = new_run_manifest(run_id, site_id, workflow_type, crawl_mode)
        mm.set_dimension(
            "launched",
            "fail",
            basis=f"agent session crashed before initializing: {error[:200]}",
        )
        mp.parent.mkdir(parents=True, exist_ok=True)
        mm.save(mp)
        return True
    except Exception:  # noqa: BLE001 — marker is best-effort, never raise
        return False


def _parse_outcome_line(text: str):
    """Return (outcome, site_id, run_id, record_count, reason) from the LAST
    matching line, or None."""
    if not text:
        return None
    for line in reversed(text.splitlines()):
        m = OUTCOME_LINE_RE.search(line.strip())
        if m:
            rc = int(m.group(4)) if m.group(4) else None
            return (
                m.group(1).upper(),
                m.group(2),
                m.group(3),
                rc,
                (m.group(5) or "").strip() or None,
            )
    return None


def _build_prompt(site_id: str, run_id: str, workflow_type: str) -> str:
    return (
        f"Run the validated workflow for site '{site_id}' at FULL scope. run_id = {run_id}.\n\n"
        f"Execute workflow.py (CRAWL_MODE=full), monitor it to completion, keep the data under "
        f"runs/{run_id}/, record each completion dimension in the manifest (the outcome is "
        f"gate-computed), and feed any genuine full-scale problem back to /explore.\n\n"
        f"Suggested spine (adapt freely — you decide poll cadence / depth):\n"
        f"  1. init_run_manifest('{site_id}', '{run_id}', workflow_type='{workflow_type}')\n"
        f"  2. Read inputs/{site_id}/goal.md\n"
        f"  3. start_crawl('{site_id}', '{run_id}')\n"
        f"  4. poll_crawl('{site_id}', '{run_id}', wait_s=240, wake_pattern='(?i)captcha|login|denied|403') "
        f"until state is terminal — alerts wake you early; kill_crawl if structurally doomed\n"
        f"  5. read_crawl_output('{site_id}', '{run_id}')\n"
        f"  6. update_run_manifest for each dimension (record_count + output_path with the output dim)\n"
        f"  7. write_run_report; append_feedback ONLY on a genuine full-scale problem\n"
        f"  8. read_run_manifest -> take its `outcome` -> emit the outcome line\n\n"
        f"**Outcome contract** (orchestrator parses via regex — verbatim):\n\n"
        f"    [COMPLETE|PARTIAL|FAILED|ABORTED] {site_id} run_id={run_id} records=<N> — <one-line reason>\n\n"
        f"COMPLETE/PARTIAL/FAILED/ABORTED MUST equal read_run_manifest's `outcome`. ERROR only if a "
        f"tool itself crashes. This MUST be your LAST line."
    )


def _build_options(
    root: Path, *, permission_mode: str, max_turns: int, model: str | None, runner_config: dict
) -> ClaudeAgentOptions:
    """Self-contained options: runner MCP server + Read only, enforced by
    tools=["Read"] — no Bash/Write/Edit/Skill in the built-in set (isolation
    by tool surface; allowed_tools alone restricts nothing). runner_config
    carries the deterministic kill thresholds."""
    kwargs: dict[str, Any] = {
        "cwd": str(root),
        "system_prompt": _build_system_prompt(runner_config.get("workflow_type", "extraction")),
        "permission_mode": permission_mode,
        "max_turns": max_turns,
        "mcp_servers": {"runner": build_runner_server(runner_config)},
        # tools (NOT allowed_tools) is what actually restricts the built-in
        # surface — allowed_tools is only the no-prompt allowlist; without
        # this, Bash/Write/Edit are live under bypassPermissions and the
        # isolation claimed above is prompt-level only. MCP tools ride
        # mcp_servers and are unaffected.
        "tools": ["Read"],
        "allowed_tools": ["Read"] + [f"mcp__runner__{t}" for t in RUNNER_TOOL_NAMES],
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


def _msg_events(msg: Any, summary: RunAgentSummary) -> list[dict[str, Any]]:
    """Normalize one SDK message into small event dicts + update summary
    (same shape as runtime.validate._msg_events)."""
    out: list[dict[str, Any]] = []
    if isinstance(msg, SystemMessage):
        out.append({"kind": "system", "subtype": getattr(msg, "subtype", None)})
    elif isinstance(msg, AssistantMessage):
        summary.turn_count += 1
        for b in msg.content:
            if isinstance(b, TextBlock):
                out.append({"kind": "assistant_text", "text": b.text})
            elif isinstance(b, ToolUseBlock):
                summary.tool_calls += 1
                summary.tool_call_breakdown[b.name] = summary.tool_call_breakdown.get(b.name, 0) + 1
                out.append({"kind": "tool_use", "name": b.name, "id": b.id, "input": b.input})
    elif isinstance(msg, UserMessage):
        for b in msg.content if isinstance(msg.content, list) else []:
            if isinstance(b, ToolResultBlock):
                out.append(
                    {
                        "kind": "tool_result",
                        "tool_use_id": getattr(b, "tool_use_id", None),
                        "content": getattr(b, "content", None),
                        "is_error": getattr(b, "is_error", False),
                    }
                )
    elif isinstance(msg, ResultMessage):
        summary.finished = True
        usage = getattr(msg, "usage", {}) or {}
        # DeepSeek runs: recompute cost from usage (the SDK overstates ~25-40x).
        # Shared by the production Run agent AND the agentic crawl agent (which
        # reuses this _msg_events).
        summary.total_cost_usd = pricing.effective_cost(
            usage, getattr(msg, "total_cost_usd", None)
        )
        summary.total_input_tokens = usage.get("input_tokens")
        summary.total_output_tokens = usage.get("output_tokens")
        out.append({"kind": "result", "cost_usd": summary.total_cost_usd})
    return out


async def run_agent(
    site_id: str,
    *,
    run_id: str | None = None,
    project_root: Path | str | None = None,
    permission_mode: str = "bypassPermissions",
    max_turns: int = 40,
    model: str | None = None,
    crawl_mode: str = "full",
    wall_clock_cap_s: float = run_exec.DEFAULT_WALL_CLOCK_CAP_S,
    stall_timeout_s: float = run_exec.DEFAULT_STALL_TIMEOUT_S,
    output_root: str | None = None,
    python_exe: str | None = None,
    steer_poll_s: float = 1.0,
    steer_offset: int | None = None,
    summary: RunAgentSummary | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Spawn a self-contained Run agent SDK session. Yields normalized event
    dicts; updates `summary` in place (outcome/run_id/record_count/reason from
    the final outcome line, which should equal the manifest's computed outcome)."""
    if summary is None:
        summary = RunAgentSummary()
    summary.site_id = site_id
    summary.crawl_mode = crawl_mode
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    summary.run_id = run_id

    assert_api_key()
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent
    workflow_type = run_exec.detect_workflow_type(site_id)
    summary.workflow_type = workflow_type

    runner_config = {
        "workflow_type": workflow_type,
        "crawl_mode": crawl_mode,
        "wall_clock_cap_s": wall_clock_cap_s,
        "stall_timeout_s": stall_timeout_s,
        "output_root": output_root,
        "python_exe": python_exe,
    }
    options = _build_options(
        root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        runner_config=runner_config,
    )
    prompt = _build_prompt(site_id, run_id, workflow_type)

    rl = RunLogger(
        role="run",
        site_id=site_id,
        config={
            "model": model,
            "max_turns": max_turns,
            "run_id": run_id,
            "workflow_type": workflow_type,
            "crawl_mode": crawl_mode,
            "wall_clock_cap_s": wall_clock_cap_s,
            "stall_timeout_s": stall_timeout_s,
            "output_root": output_root,
            "permission_mode": permission_mode,
        },
    )
    summary.run_log_path = str(rl.path)

    # ── live mid-run steering ────────────────────────────────────────────
    # Tail workspaces/<id>/user_run_steering.md (the backend appends operator
    # messages there) and feed each new block to the LIVE agent via SDK
    # streaming-input — mirrors runtime.run's explore steering, so the user can
    # converse with the Run agent while the crawl is in flight. We start at EOF
    # so a PRESERVED workspace's prior steering is not replayed into this run.
    steer_queue: asyncio.Queue = asyncio.Queue()
    steer_path = run_exec._workspace(site_id) / "user_run_steering.md"
    _input_closed = False

    def _close_input() -> None:
        nonlocal _input_closed
        if not _input_closed:
            _input_closed = True
            try:
                steer_queue.put_nowait(None)  # exhaust the iterable so query() returns
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

    # Default: start at EOF so a PRESERVED workspace's prior steering is not
    # replayed. A caller that files a message and THEN starts this run passes
    # `steer_offset` (the pre-append size) so that message IS delivered — the
    # "chat with a finished run auto-reruns it, carrying the message" path.
    if steer_offset is not None:
        _steer_offset = max(0, int(steer_offset))
    else:
        try:
            _steer_offset = steer_path.stat().st_size if steer_path.is_file() else 0
        except OSError:
            _steer_offset = 0

    async def _tail_user_run_steering() -> None:
        nonlocal _steer_offset
        while True:
            await asyncio.sleep(steer_poll_s)
            try:
                if not steer_path.is_file():
                    continue
                if steer_path.stat().st_size <= _steer_offset:
                    continue
                with steer_path.open("r", encoding="utf-8") as f:
                    f.seek(_steer_offset)
                    new = f.read()
                    _steer_offset = f.tell()
            except asyncio.CancelledError:
                raise
            except OSError:
                continue
            # Strip the backend's "## Operator steering @ <ts>" block headers
            # (provenance lives in the wrapper below), mirroring explore_loop's
            # _strip_steer_headers.
            body = "\n".join(
                ln for ln in new.splitlines() if not ln.strip().startswith("## Operator steering @")
            ).strip()
            if body:
                steer_queue.put_nowait(
                    "⚑ OPERATOR MESSAGE (live, mid-crawl) — the user watching this "
                    "run is talking to you. Reply FIRST with send_user_message("
                    f"'{site_id}', '{run_id}', ...) — acknowledge what they asked "
                    "AND answer it / say concretely how you're adjusting (or why "
                    "you won't); a bare \"OK\" doesn't count; fold multiple "
                    "messages into one reply. It does NOT pause the crawl — the "
                    "subprocess keeps running; reply, adapt your monitoring/"
                    "reporting if asked, then continue.\n\n" + body
                )

    steer_task = asyncio.create_task(_tail_user_run_steering())

    chunks: list[str] = []
    try:
        async for msg in query(prompt=_input_stream(), options=options):
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
                # Agent finished this run → exhaust the streaming input so
                # query() returns (otherwise it waits forever for more steering).
                _close_input()
        rl.write({"event": "run_complete", "summary": summary.to_dict()})
    except Exception as exc:  # noqa: BLE001 — surface as event
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
        # Stop the steering tail + close the input iterable (so a never-emitted
        # ResultMessage on the error path can't hang the SDK transport teardown).
        _close_input()
        steer_task.cancel()
        try:
            await steer_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        # Best-effort: don't leave a full crawl (+ headless chromium) running if
        # the agent ended early / errored mid-crawl.
        try:
            await run_exec.cleanup_run(site_id, run_id)
        except Exception:  # noqa: BLE001
            pass

    combined = "\n".join(chunks).rstrip()
    parsed = _parse_outcome_line(combined)
    if parsed is not None:
        outcome, parsed_site, parsed_run, rc, reason = parsed
        summary.outcome = outcome
        summary.outcome_line = next(
            (ln for ln in reversed(combined.splitlines()) if OUTCOME_LINE_RE.search(ln)), None
        )
        summary.run_id = parsed_run or run_id
        summary.record_count = rc
        summary.reason = reason
        if parsed_site and parsed_site != site_id:
            summary.error = (
                f"outcome line site_id mismatch: contract said '{site_id}', "
                f"model wrote '{parsed_site}'"
            )
    else:
        # No parseable outcome line. A session that ERRORED may have died before
        # init_run_manifest — leave a crash-marker manifest so the run is not
        # invisible to the next explore's feedback hook.
        if summary.error is not None:
            _write_crash_marker(site_id, run_id, workflow_type, crawl_mode, summary.error)
        # For a CLEAN session, fall back to the
        # manifest before declaring ABORTED — its outcome is gate-COMPUTED (the
        # line is only a restatement), and a model once dropped the [brackets]
        # turning a COMPLETE run into a displayed ABORTED.
        manifest_outcome: str | None = None
        manifest_reason: str | None = None
        if summary.error is None:
            try:
                from mcp_server.schemas import RunManifestFile

                m = RunManifestFile.load(
                    run_exec._workspace(site_id) / "runs" / run_id / "manifest.yaml"
                )
                mo = (m.outcome or "").upper()
                if mo in ("COMPLETE", "PARTIAL", "FAILED", "ABORTED"):
                    manifest_outcome = mo
                    if summary.record_count is None:
                        summary.record_count = m.record_count
                    # Assemble a reason from the failing gating dims so the
                    # summary never carries a bare outcome with no text.
                    fails = [
                        f"{n}: {d.basis or 'failed'}"
                        for n, d in m.dimensions.items()
                        if d.gating and d.status == "fail"
                    ]
                    if fails:
                        manifest_reason = "; ".join(fails)[:300]
            except Exception:  # noqa: BLE001 — fallback only, never raise here
                pass
        if manifest_outcome is not None:
            summary.outcome = manifest_outcome
            summary.reason = manifest_reason or (
                "outcome taken from manifest (no parseable outcome line)"
            )
        else:
            summary.outcome = "ERROR" if summary.error else "ABORTED"
            summary.reason = summary.error or "no outcome line emitted by run session"


__all__ = ["RunAgentSummary", "run_agent"]
