"""Orchestrator: drive /explore <-> validation-agent loop until PASS or budget hits.

Replaces the manual "/explore then call validation-agent then maybe re-explore"
flow with a Python state machine that:

1. Spawns each iter's /explore as a fresh Claude Agent SDK session — every
   iter starts with a clean LLM context, so context decay across many
   PROBE turns doesn't compound.
2. Carries forward cross-iter learning via files (workspace artifacts +
   SessionStart hook injection), NOT via in-process conversation memory.
3. Between iters: snapshots workflow.py to `workflow_history/iter_<N>_*.py`
   so the next iter's hook can inject a code diff.
4. Validation between iters is delegated to the self-contained validator
   SDK agent (`runtime.validate.validate`) — a fresh SDK session whose
   entire tool surface is the in-process validator MCP server
   (`runtime.validator_tools`); the verdict is gate-computed from its
   scorecard, so it's identical regardless of who triggers it.

**Route-aware (stage 2).** An /explore run ENDS by declaring a terminal route
(`declare_crawl_route` → workspaces/<site>/crawl_route.json — there is no [DONE]
marker anymore). After each explore session the loop reads the route and branches:

- **(none declared)** — the session is UNFINISHED: verdict INCONCLUSIVE, a loop
  notice is queued as the next iteration's first user turn ("finish by declaring
  a route"), iterate.
- **deterministic** — validate workflow.py as before (the original loop body).
- **inline** — explore harvested the small full set itself (commit_inline_dataset
  → runs/<run_id>/output.json + manifest). No workflow.py exists, so validation
  would mis-FAIL; instead a deterministic spot-check (manifest outcome=complete +
  non-empty output.json) decides PASS / iterate.
- **agentic** — explore wrote crawl_brief.md; the loop DISPATCHES the agentic
  crawl agent (runtime.crawl_agent) inline, streams its events, and maps its
  outcome: COMPLETE→PASS; PARTIAL/FAILED/ABORTED→iterate (the next explore reads
  runs/<run_id>/feedback.yaml via the inject_run_feedback hook and refines);
  ERROR→loop ERROR.
- **infeasible** — terminal INCONCLUSIVE (off_goal_report.yaml carries the
  first-hand description upstream); no iteration.

Exit conditions (loop terminates on first hit):

- **PASS** — validation aggregate says PASS (deterministic), inline spot-check
  passed, or the dispatched agentic crawl COMPLETEd
- **route_infeasible** — explore declared the source uncrawlable
- **max_iters** — hard cap reached, return whatever last verdict was
- **max_cost** — cumulative LLM cost exceeds budget (incl. dispatched crawl cost)
- **stuck** — last 2 iter_summary `next_strategy` fields look identical (LLM judge)
- **explore_failed** — /explore SDK session crashed
- **validation_crashed** — the validator SDK session errored

Usage from CLI: `python -m runtime.cli explore-loop <site_id>` (see cli.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from runtime.run import RunSummary, explore
from runtime.validate import ValidationSummary, validate


# ─────────────────────────── data ──────────────────────────────────


@dataclass
class IterRecord:
    """One iter's collected facts: explore cost + validation verdict."""

    iter_n: int
    started_at: str
    explore_summary: RunSummary | None = None
    workflow_snapshot_path: Path | None = None
    validation_summary: ValidationSummary | None = None
    verdict: Literal["PASS", "FAIL", "INCONCLUSIVE", "ERROR"] | None = None
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    error: str | None = None
    route: str | None = None  # declared terminal route (crawl_route.json), if any
    crawl_run_id: str | None = None  # agentic dispatch: the crawl's run id
    crawl_outcome: str | None = None  # agentic dispatch: COMPLETE/PARTIAL/FAILED/ABORTED/ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "iter_n": self.iter_n,
            "started_at": self.started_at,
            "verdict": self.verdict,
            "route": self.route,
            "crawl_run_id": self.crawl_run_id,
            "crawl_outcome": self.crawl_outcome,
            "cost_usd": round(self.cost_usd, 4),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "workflow_snapshot": (
                str(self.workflow_snapshot_path) if self.workflow_snapshot_path else None
            ),
            "explore_turn_count": (
                self.explore_summary.turn_count if self.explore_summary else None
            ),
            "explore_tool_calls": (
                self.explore_summary.tool_calls if self.explore_summary else None
            ),
            "explore_cost_usd": (
                round(self.explore_summary.total_cost_usd, 4)
                if self.explore_summary and self.explore_summary.total_cost_usd is not None
                else None
            ),
            "validation": (self.validation_summary.to_dict() if self.validation_summary else None),
            "error": self.error,
        }


@dataclass
class LoopSummary:
    """End-of-loop outcome."""

    site_id: str
    iterations: list[IterRecord] = field(default_factory=list)
    final_verdict: str = "UNSET"
    exit_reason: str = ""
    total_cost_usd: float = 0.0
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "final_verdict": self.final_verdict,
            "exit_reason": self.exit_reason,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "iterations_run": len(self.iterations),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "iterations": [i.to_dict() for i in self.iterations],
        }


# ─────────────────────────── helpers ───────────────────────────────


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utcnow_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _workspace_dir(site_id: str) -> Path:
    return PROJECT_ROOT / "workspaces" / site_id


# The artifact set validation actually judges (all workflow types). The
# skip-when-unchanged check below hashes whichever of these exist; any byte
# change in any of them forces a fresh validation.
_VALIDATED_ARTIFACTS = (
    "workflow.py",
    "selectors.yaml",
    "download_manifest.yaml",
    "api_manifest.yaml",
    "output_sample.json",
)


def _validated_artifacts_hash(site_id: str) -> str | None:
    """Combined sha256 over the validated artifact set; None when none of the
    files exist yet (always validate then — there is nothing to reuse)."""
    ws = _workspace_dir(site_id)
    h = hashlib.sha256()
    found = False
    for name in _VALIDATED_ARTIFACTS:
        try:
            data = (ws / name).read_bytes()
        except OSError:
            continue
        found = True
        h.update(name.encode("utf-8"))
        h.update(len(data).to_bytes(8, "little"))
        h.update(data)
    return h.hexdigest() if found else None


# ── terminal-route resolution (the run's exit contract) ──────────────

_KNOWN_ROUTES = ("deterministic", "agentic", "inline", "infeasible")


def _read_crawl_route(site_id: str) -> dict[str, Any] | None:
    """workspaces/<site>/crawl_route.json as a dict, or None when absent /
    unparseable / carrying an unknown route — all three mean 'no usable route
    was declared' to the loop."""
    p = _workspace_dir(site_id) / "crawl_route.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("route") not in _KNOWN_ROUTES:
        return None
    return data


def _check_inline_artifact(site_id: str, run_id: str | None) -> tuple[bool, str]:
    """Deterministic spot-check of the inline route's deliverable (no LLM):
    runs/<run_id>/manifest.yaml gate-computes COMPLETE and output.json is a
    non-empty record list. Returns (ok, detail)."""
    if not run_id:
        return False, (
            "crawl_route.json declares route=inline but carries no run_id — "
            "commit_inline_dataset was never called (declaring inline does not "
            "commit the data)"
        )
    run_dir = _workspace_dir(site_id) / "runs" / run_id
    try:
        from mcp_server.schemas import RunManifestFile

        m = RunManifestFile.load(run_dir / "manifest.yaml")
    except Exception as exc:  # noqa: BLE001 — missing/corrupt manifest = check fails
        return False, f"inline run {run_id}: manifest unreadable ({type(exc).__name__}: {exc})"
    if (m.outcome or "").lower() != "complete":
        return False, f"inline run {run_id}: manifest outcome={m.outcome} (need complete)"
    out = run_dir / "output.json"
    try:
        records = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"inline run {run_id}: output.json unreadable ({type(exc).__name__})"
    if not isinstance(records, list) or not records:
        return False, f"inline run {run_id}: output.json is not a non-empty record list"
    return True, f"{len(records)} records committed inline (run {run_id})"


def _crawl_outcome_to_verdict(outcome: str | None) -> str:
    """Map the dispatched agentic crawl's outcome to a loop verdict. COMPLETE is
    the only PASS; an incomplete harvest (PARTIAL/FAILED/ABORTED) iterates — the
    next explore reads the run's feedback.yaml (inject_run_feedback hook) and
    refines the brief or pivots route; ERROR is machinery."""
    o = (outcome or "").upper()
    if o == "COMPLETE":
        return "PASS"
    if o in ("PARTIAL", "FAILED", "ABORTED"):
        return "FAIL"
    return "ERROR"


# ── live mid-run steering: tail user_steering.md → the iteration's queue ──

_STEER_POLL_SECONDS = 0.7


def _strip_steer_headers(text: str) -> str:
    """Drop the backend's `## Operator steering @ <ts>` block headers, keep the
    guidance body."""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("## Operator steering @")]
    return "\n".join(lines).strip()


async def _tail_user_steering(path: Path, queue: "asyncio.Queue", start_offset: int) -> None:
    """Poll `user_steering.md` and push NEWLY-appended steering blocks (those
    written AFTER this iteration began — `start_offset`) into `queue` for live,
    turn-level delivery to the running /explore agent. Content already present at
    SessionStart is skipped (it's already in the agent's context). Best-effort:
    tailing must never crash the loop. Cancelled when the iteration ends."""
    offset = start_offset
    while True:
        try:
            await asyncio.sleep(_STEER_POLL_SECONDS)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size <= offset:
                continue
            with path.open("rb") as f:
                f.seek(offset)
                raw = f.read(size - offset)
            text = raw.decode("utf-8", errors="replace")
            # Consume only up to the last newline so a half-written block waits
            # for the next poll.
            nl = text.rfind("\n")
            if nl == -1:
                continue
            consumed = text[: nl + 1]
            offset += len(consumed.encode("utf-8"))
            content = _strip_steer_headers(consumed)
            if not content:
                continue
            queue.put_nowait(
                "⚑ OPERATOR STEERING (live, mid-run) — honor immediately: "
                "reconcile your task_plan.md and current plan with this, then "
                "continue. Reply FIRST with send_user_message — acknowledge "
                "what they asked AND say concretely how you're adjusting (or "
                "why you won't); a bare \"OK\" doesn't "
                "count; fold multiple steers into one reply. It does not pause "
                "the run — send it, then keep working.\n\n" + content
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — tailing must never crash the loop
            continue


_PENDING_BACKLOG_LIMIT = 3  # newest unacknowledged steers re-delivered at iter start


def _pending_steer_backlog(site_id: str) -> "list[tuple[str, str]]":
    """(ts, body) of operator steers the agent has never replied to — steer
    timestamp after the agent's last send_user_message in _agent_messages.jsonl
    (all steers when the agent never messaged). Mirrors the SessionStart hook's
    PENDING computation; replicated here because .claude/hooks isn't importable.
    Best-effort: returns [] on any error."""
    try:
        import re

        ws = _workspace_dir(site_id)
        steering = ws / "user_steering.md"
        if not steering.exists():
            return []
        text = steering.read_text(encoding="utf-8", errors="replace")
        matches = list(re.finditer(r"^## Operator steering @ (.+?)\s*$", text, re.MULTILINE))
        blocks: list[tuple[datetime, str]] = []
        for i, m in enumerate(matches):
            try:
                ts = datetime.fromisoformat(m.group(1).strip())
            except ValueError:
                continue
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end() : end].strip()
            if body:
                blocks.append((ts, body))
        if not blocks:
            return []
        last_reply: datetime | None = None
        msgs = ws / "_agent_messages.jsonl"
        if msgs.exists():
            for line in msgs.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                    ts = datetime.fromisoformat(str(rec.get("created_at")))
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if last_reply is None or ts > last_reply:
                    last_reply = ts
        pending = [b for b in blocks if last_reply is None or b[0] > last_reply]
        pending.sort(key=lambda b: b[0])
        return [
            (ts.isoformat(timespec="seconds").replace("+00:00", "Z"), body)
            for ts, body in pending[-_PENDING_BACKLOG_LIMIT:]
        ]
    except Exception:  # noqa: BLE001 — backlog delivery must never crash the loop
        return []


def _queue_pending_steer_backlog(site_id: str, queue: "asyncio.Queue") -> None:
    """Re-deliver unacknowledged steers as a TURN-LEVEL user message at iteration
    start. The SessionStart hook already injects them as the PENDING context
    block, but under a heavy injected context a task script can out-compete that
    block for attention (observed 2026-06-09: fresh workspace complied in 25s,
    iter-9 workspace skipped straight to its scripted task). A queued message
    arrives as an actual user turn — much harder to skip. Redundant by design;
    one send_user_message reply clears both (same timeline basis)."""
    pending = _pending_steer_backlog(site_id)
    if not pending:
        return
    items = "\n".join(f"- [{ts}] {body}" for ts, body in pending)
    queue.put_nowait(
        "⚑ OPERATOR STEERING (pending backlog, re-delivered at iteration start) — "
        "the operator sent these earlier and has had NO reply yet. Honor them: "
        "reconcile your task_plan.md and current plan with them, then continue. "
        "Reply FIRST with send_user_message — acknowledge what they asked AND say "
        "concretely how you're adjusting (or why you won't); a bare \"OK\" doesn't "
        "count; fold ALL of them into ONE reply. It does not pause the run — send "
        "it, then keep working.\n\n" + items
    )


def _snapshot_workflow(site_id: str, iter_n: int) -> Path | None:
    """Copy workflow.py to workflow_history/iter_NN_<ts>.py. Returns dest path
    or None if workflow.py doesn't exist yet (first iter starting fresh)."""
    src = _workspace_dir(site_id) / "workflow.py"
    if not src.exists():
        return None
    history_dir = _workspace_dir(site_id) / "workflow_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    dst = history_dir / f"iter_{iter_n:02d}_{_utcnow_compact()}.py"
    shutil.copy2(src, dst)
    return dst


def _input_fingerprint(site_id: str) -> dict[str, Any]:
    """SHA256 (truncated) of the site's goal.md + seed.json / api_spec.json so a
    persisted loop summary records exactly which inputs produced this run."""
    d = PROJECT_ROOT / "inputs" / site_id
    out: dict[str, Any] = {}
    for name in ("goal.md", "seed.json", "api_spec.json"):
        f = d / name
        try:
            out[name] = hashlib.sha256(f.read_bytes()).hexdigest()[:16] if f.exists() else None
        except Exception:
            out[name] = None
    return out


def _persist_loop_summary(
    summary: "LoopSummary", *, model: str | None, max_iters: int, max_cost_usd: float
) -> Path | None:
    """Write LoopSummary + run-config + git + input fingerprint to
    workspaces/<site>/loop_summary.json. Best-effort; never raises. This is the
    durable, machine-readable record of the loop's outcome (previously the
    LoopSummary lived only in the ephemeral stdout stream)."""
    from runtime.run_logger import git_state

    try:
        ws = _workspace_dir(summary.site_id)
        ws.mkdir(parents=True, exist_ok=True)
        payload = {
            **summary.to_dict(),
            "config": {"model": model, "max_iters": max_iters, "max_cost_usd": max_cost_usd},
            "git": git_state(),
            "inputs": _input_fingerprint(summary.site_id),
        }
        out = ws / "loop_summary.json"
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return out
    except Exception:
        return None


def _derive_verdict(
    explore_summary: RunSummary | None,
    validation_summary: ValidationSummary | None,
) -> str:
    """Compose final PASS/FAIL/INCONCLUSIVE/ERROR.

    **Hard rules (deterministic Python, no LLM)**: only "the system itself
    broke" cases — SDK session crashes, missing validation. These do not
    depend on site-specific data and are kept in code on purpose:

      - explore SDK session crashed before finishing → ERROR
      - validation SDK session crashed without emitting verdict → ERROR
      - validation never ran → INCONCLUSIVE

    **Site-specific rules** (any_old_reddit_url, post_body_non_null, exit
    codes, goal.md acceptable-outcomes table, etc.) are NOT hard-coded
    here anymore — the validation SDK session reads goal.md and judges
    them itself, then emits a single verdict line. We just trust its
    PASS/FAIL/INCONCLUSIVE call.

    This replaces the earlier `_derive_verdict(standalone, verify)` which
    hard-coded reddit-trump rules in Python — see session essence
    2026-05-23 "硬编码 verdict" entry for the design discussion.
    """
    # Hard rule 1: explore session crashed
    if explore_summary is not None and explore_summary.error and not explore_summary.finished:
        return "ERROR"

    # Hard rule 2: no validation at all
    if validation_summary is None:
        return "INCONCLUSIVE"

    # Hard rule 3: validation session crashed without producing a verdict
    if validation_summary.error and validation_summary.verdict in (None, "UNSET"):
        return "ERROR"

    # Trust the LLM verdict otherwise
    v = (validation_summary.verdict or "").upper()
    if v in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR"):
        return v
    return "INCONCLUSIVE"


def _stuck_check(iters: list[IterRecord]) -> bool:
    """Detect if last 2 iters' iter_summary `Recommended next strategy` are
    too similar (suggesting agent stuck retrying same approach).

    Cheap signature: strip whitespace + lowercase, compare. Real solution
    would use embeddings; for v1 this catches the most obvious cases
    (agent literally writes the same next_strategy sentence twice).
    """
    if len(iters) < 2:
        return False
    site_id = None  # not strictly needed; we look at iter_summary on disk
    # Find the workspace via the iters' workflow snapshots
    for it in iters[::-1]:
        if it.workflow_snapshot_path:
            site_id = it.workflow_snapshot_path.parent.parent.name
            break
    if not site_id:
        return False
    summary_path = _workspace_dir(site_id) / "iter_summary.md"
    if not summary_path.exists():
        return False
    text = summary_path.read_text(encoding="utf-8")
    # Find last 2 "## Recommended next strategy" sections
    import re

    matches = list(re.finditer(r"^## Recommended next strategy\s*$", text, re.MULTILINE))
    if len(matches) < 2:
        return False
    last = text[matches[-1].end() : matches[-1].end() + 800].strip().lower()
    prev = text[matches[-2].end() : matches[-1].start()].strip().lower()
    # Take first 200 chars of each (the most committed strategy lines)
    last_sig = "".join(last[:200].split())
    prev_sig = "".join(prev[:200].split())
    if not last_sig or not prev_sig:
        return False
    # Ordered-sequence similarity, NOT per-character set membership: any two
    # English strategies share nearly the whole alphabet, so a set-overlap ratio
    # is ~1.0 for unrelated text and would fire "stuck" every iter. difflib's
    # ratio compares matching SUBSEQUENCES, so only genuinely-similar strategies
    # (the real "we keep proposing the same fix" case) clear the threshold.
    import difflib

    similarity = difflib.SequenceMatcher(None, last_sig, prev_sig).ratio()
    return similarity > 0.80


# ─────────────────────────── main loop ─────────────────────────────


async def explore_until_pass(
    site_id: str,
    *,
    max_iters: int = 5,
    max_cost_usd: float = 20.0,
    model: str | None = None,
    on_event: Any = None,  # optional callback(dict) for live progress
) -> LoopSummary:
    """Reactive loop: /explore → snapshot → validate → if FAIL re-explore.

    Each iter runs in a **fresh** Claude Agent SDK session — cross-iter
    learning flows through workspace files (iter_summary.md, hypotheses.yaml,
    workflow_history/) injected by the SessionStart hook.
    """
    summary = LoopSummary(site_id=site_id, started_at=_utcnow_iso())

    # Validation skip-when-unchanged state (see step 3 inside the loop).
    last_validated_hash: str | None = None
    last_val_summary: ValidationSummary | None = None

    # Carry-over notice from the previous iteration's route handling (ended
    # without a route / inline check failed / dispatched crawl didn't COMPLETE).
    # Delivered as the next iteration's FIRST user turn via the steering queue.
    loop_notice: str | None = None

    for n in range(1, max_iters + 1):
        rec = IterRecord(iter_n=n, started_at=_utcnow_iso())
        summary.iterations.append(rec)
        t_start = asyncio.get_event_loop().time()

        if on_event:
            on_event({"kind": "iter_start", "iter_n": n, "site_id": site_id})

        # ─── 1. Run /explore with active_site env var set ───
        # Setting CRAWLER_EXPLORER_ACTIVE_SITE in os.environ; the
        # SessionStart hook reads it to inject per-workspace context.
        # The Claude Agent SDK subprocess inherits os.environ, so we
        # set it before query() spawns the CLI.
        os.environ["CRAWLER_EXPLORER_ACTIVE_SITE"] = site_id
        # Iter provenance for the SessionStart conversation-history injection:
        # (b) stamp the iter into env so send_user_message tags each agent→user
        # message exactly; (a) record this iter's start time in a ledger so the
        # hook can derive the iter for operator-steering blocks (which the backend
        # writes without an iter number) by timestamp window.
        os.environ["CRAWLER_EXPLORER_ITER_N"] = str(n)
        try:
            _ledger = _workspace_dir(site_id) / "_iter_ledger.jsonl"
            _ledger.parent.mkdir(parents=True, exist_ok=True)
            with _ledger.open("a", encoding="utf-8") as _lf:
                _lf.write(json.dumps({"iter": n, "started_at": rec.started_at}) + "\n")
        except OSError:
            pass
        explore_summary = RunSummary()
        # Live mid-run steering: tail this site's user_steering.md (the same file
        # the backend's /steer endpoint appends to) and feed newly-appended blocks
        # to the running /explore agent at turn boundaries. Offset = current size
        # so the SessionStart-injected content isn't re-delivered live.
        steer_path = _workspace_dir(site_id) / "user_steering.md"
        steer_offset = steer_path.stat().st_size if steer_path.exists() else 0
        steer_q: asyncio.Queue = asyncio.Queue()
        tail_task = asyncio.create_task(_tail_user_steering(steer_path, steer_q, steer_offset))
        # Unacknowledged steer backlog → first turn boundary (see the helper's
        # docstring for why this duplicates the SessionStart PENDING block).
        _queue_pending_steer_backlog(site_id, steer_q)
        # Previous iteration's route-handling notice → first user turn (same
        # channel as the backlog — proven to out-compete heavy injected context).
        if loop_notice:
            steer_q.put_nowait(
                "⚑ LOOP NOTICE (from the orchestrator, NOT the operator — no "
                "send_user_message reply needed; just act on it): " + loop_notice
            )
            loop_notice = None
        try:
            async for ev in explore(
                site_id,
                permission_mode="bypassPermissions",
                max_turns=200,
                model=model,
                summary=explore_summary,
                steer_queue=steer_q,
            ):
                if on_event:
                    on_event({"kind": "explore_event", "iter_n": n, "event": ev})
        except Exception as exc:  # noqa: BLE001
            rec.error = f"explore crashed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            rec.verdict = "ERROR"
            rec.elapsed_seconds = asyncio.get_event_loop().time() - t_start
            summary.exit_reason = "explore_failed"
            summary.final_verdict = "ERROR"
            break
        finally:
            tail_task.cancel()
            try:
                await tail_task
            except asyncio.CancelledError:
                pass
            os.environ.pop("CRAWLER_EXPLORER_ACTIVE_SITE", None)
            os.environ.pop("CRAWLER_EXPLORER_ITER_N", None)

        rec.explore_summary = explore_summary
        rec.cost_usd = float(explore_summary.total_cost_usd or 0.0)
        summary.total_cost_usd += rec.cost_usd

        # ─── 2. Snapshot workflow.py for next iter's diff ───
        try:
            rec.workflow_snapshot_path = _snapshot_workflow(site_id, n)
            if on_event:
                on_event(
                    {
                        "kind": "snapshot",
                        "iter_n": n,
                        "path": str(rec.workflow_snapshot_path)
                        if rec.workflow_snapshot_path
                        else None,
                    }
                )
        except OSError as exc:
            # Non-fatal — log + continue
            if on_event:
                on_event({"kind": "snapshot_failed", "iter_n": n, "error": str(exc)})

        # ─── 3. Resolve the declared terminal route — the run's EXIT contract ───
        # An /explore run ends by declare_crawl_route (no [DONE] marker). The
        # loop branches on workspaces/<site>/crawl_route.json; only the
        # deterministic route goes through the validator.
        route_info = _read_crawl_route(site_id)
        route = (route_info or {}).get("route")
        rec.route = route
        if on_event:
            on_event(
                {
                    "kind": "route_resolved",
                    "iter_n": n,
                    "route": route,
                    "rationale": (route_info or {}).get("rationale"),
                }
            )

        if route is None:
            # Clean session, no route declared → unfinished per the exit
            # contract. Iterate with a first-turn notice (no validation — there
            # is nothing routed to validate).
            rec.verdict = "INCONCLUSIVE"
            loop_notice = (
                "your previous iteration ended WITHOUT declaring a terminal route "
                "— an /explore run ends with declare_crawl_route(...); there is no "
                "other way to finish. Decide now (tiny full set → "
                "commit_inline_dataset; too-dynamic for static code → "
                "write_crawl_brief then declare agentic; uncrawlable → "
                "report_off_goal then declare infeasible; otherwise finalize the "
                "deterministic artifacts and declare deterministic) and END with "
                "the declaration."
            )

        elif route == "infeasible":
            # Explore judged the source uncrawlable (off_goal_report.yaml carries
            # the first-hand description). Terminal — exit decision below breaks.
            rec.verdict = "INCONCLUSIVE"

        elif route == "inline":
            # Explore harvested the small full set itself. No workflow.py exists
            # (validation would mis-FAIL); a deterministic spot-check of the
            # committed deliverable decides instead.
            ok, detail = _check_inline_artifact(site_id, (route_info or {}).get("run_id"))
            if on_event:
                on_event({"kind": "inline_check", "iter_n": n, "ok": ok, "detail": detail})
            rec.verdict = "PASS" if ok else "FAIL"
            if not ok:
                loop_notice = (
                    f"route=inline was declared but the deliverable check failed: "
                    f"{detail}. Commit the FULL set via commit_inline_dataset, or "
                    "pivot the route if inline was the wrong call."
                )

        elif route == "agentic":
            # Explore wrote the brief; the loop dispatches the agentic crawl
            # agent inline and maps its outcome to the verdict.
            brief_path = _workspace_dir(site_id) / "crawl_brief.md"
            if not brief_path.exists():
                rec.verdict = "FAIL"
                loop_notice = (
                    "route=agentic was declared but crawl_brief.md does not exist "
                    "— write_crawl_brief(...) is the agentic route's required "
                    "handoff (the crawl agent reads it)."
                )
            else:
                if on_event:
                    on_event({"kind": "crawl_start", "iter_n": n, "site_id": site_id})
                from runtime.crawl_agent import crawl_agent
                from runtime.run_agent import RunAgentSummary

                crawl_summary = RunAgentSummary()
                try:
                    async for ev in crawl_agent(site_id, model=model, summary=crawl_summary):
                        if on_event:
                            on_event({"kind": "crawl_event", "iter_n": n, "event": ev})
                except Exception as exc:  # noqa: BLE001
                    rec.error = (
                        f"agentic crawl crashed: {type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}"
                    )
                    rec.verdict = "ERROR"
                else:
                    crawl_cost = float(crawl_summary.total_cost_usd or 0.0)
                    rec.cost_usd += crawl_cost
                    summary.total_cost_usd += crawl_cost
                    rec.crawl_run_id = crawl_summary.run_id
                    rec.crawl_outcome = crawl_summary.outcome
                    rec.verdict = _crawl_outcome_to_verdict(crawl_summary.outcome)
                    if on_event:
                        on_event(
                            {
                                "kind": "crawl_done",
                                "iter_n": n,
                                "outcome": crawl_summary.outcome,
                                "run_id": crawl_summary.run_id,
                                "records": crawl_summary.record_count,
                                "reason": crawl_summary.reason,
                                "cost_usd": crawl_cost,
                            }
                        )
                    if rec.verdict == "FAIL":
                        loop_notice = (
                            f"the dispatched agentic crawl ended "
                            f"{crawl_summary.outcome} (run {crawl_summary.run_id}): "
                            f"{crawl_summary.reason or 'see runs/<run_id>/'}. Its "
                            "feedback is injected at your session start — refine "
                            "crawl_brief.md (or pivot the route), then declare again."
                        )

        else:  # route == "deterministic" — the original validate path
            # Spawns the self-contained validator SDK agent
            # (`runtime.validate.validate`). Its entire tool surface is the
            # in-process validator MCP server (mcp__validator__* tools — the
            # extraction or download subset, chosen by workflow type) + read-only
            # Read; it records each dimension in validation/<run_id>/scorecard.yaml
            # and emits a single gate-computed verdict line we parse in ValidationSummary.
            #
            # Skip-when-unchanged: validation judges a fixed artifact set
            # (workflow.py + manifests + output_sample.json). When an iter ended
            # without touching ANY of them — the agent iterated purely on
            # hypotheses — re-validating re-runs workflow.py and re-fetches the
            # live page only to reproduce the previous verdict, at $0.3-0.8 a
            # time. Reuse the previous summary instead (never reuse ERROR: a
            # crashed validator says nothing about the artifacts).
            wf_hash = _validated_artifacts_hash(site_id)
            if (
                wf_hash is not None
                and wf_hash == last_validated_hash
                and last_val_summary is not None
                and (last_val_summary.verdict or "UNSET") not in ("UNSET", "ERROR")
            ):
                if on_event:
                    on_event(
                        {
                            "kind": "validation_skipped",
                            "iter_n": n,
                            "reason": "validated artifacts unchanged since last validation",
                            "verdict": last_val_summary.verdict,
                        }
                    )
                val_summary = last_val_summary
                val_cost = 0.0
            else:
                if on_event:
                    on_event({"kind": "validation_start", "iter_n": n})
                os.environ["CRAWLER_EXPLORER_ACTIVE_SITE"] = site_id
                val_summary = ValidationSummary()
                try:
                    async for ev in validate(
                        site_id,
                        permission_mode="bypassPermissions",
                        max_turns=40,  # 11-tool orchestration (init + 6 checks + scorecard + report/feedback)
                        model=model,
                        summary=val_summary,
                    ):
                        if on_event:
                            on_event({"kind": "validate_event", "iter_n": n, "event": ev})
                except Exception as exc:  # noqa: BLE001
                    val_summary.error = (
                        f"validate crashed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                    )
                    if val_summary.verdict in ("UNSET", None):
                        val_summary.verdict = "ERROR"
                finally:
                    os.environ.pop("CRAWLER_EXPLORER_ACTIVE_SITE", None)
                val_cost = float(val_summary.total_cost_usd or 0.0)
                last_validated_hash = wf_hash
                last_val_summary = val_summary

            rec.validation_summary = val_summary
            # Validation session cost adds to the loop's budget (0 when skipped)
            rec.cost_usd += val_cost
            summary.total_cost_usd += val_cost

            rec.verdict = _derive_verdict(rec.explore_summary, val_summary)

        rec.elapsed_seconds = asyncio.get_event_loop().time() - t_start

        if on_event:
            on_event(
                {
                    "kind": "iter_done",
                    "iter_n": n,
                    "verdict": rec.verdict,
                    "cost_usd": rec.cost_usd,
                    "elapsed_seconds": rec.elapsed_seconds,
                    "cumulative_cost": summary.total_cost_usd,
                }
            )

        # ─── 4. Exit decision ───
        if route == "infeasible":
            # Explore's judgment is the source can't be crawled — iterating
            # would just ask the same question again.
            summary.final_verdict = "INCONCLUSIVE"
            rationale = ((route_info or {}).get("rationale") or "").strip()
            summary.exit_reason = (
                f"route_infeasible: {rationale[:160]}" if rationale else "route_infeasible"
            )
            break
        if rec.verdict == "PASS":
            summary.final_verdict = "PASS"
            summary.exit_reason = (
                "passed" if route == "deterministic" else f"passed (route={route})"
            )
            break
        if summary.total_cost_usd > max_cost_usd:
            summary.final_verdict = rec.verdict
            summary.exit_reason = (
                f"cost_cap_hit (${summary.total_cost_usd:.2f} > ${max_cost_usd:.2f})"
            )
            break
        if rec.verdict == "ERROR":
            summary.final_verdict = "ERROR"
            summary.exit_reason = (
                "agentic_crawl_crashed"
                if route == "agentic" and rec.error
                else "validation_or_explore_errored"
            )
            break
        if n >= 2 and _stuck_check(summary.iterations):
            summary.final_verdict = rec.verdict
            summary.exit_reason = "stuck (last 2 iters' next_strategy too similar)"
            break

    else:
        # for-loop completed without break = hit max_iters
        last_verdict = summary.iterations[-1].verdict if summary.iterations else "UNSET"
        summary.final_verdict = last_verdict or "UNSET"
        summary.exit_reason = f"max_iters_hit ({max_iters})"

    # Close the pooled validator chromium (runtime.validator_browser) — the
    # loop owns the process lifecycle for in-loop validations; without this a
    # long gap between loop end and process exit would leave a headless
    # chromium (200-400MB) idling.
    try:
        from runtime.validator_browser import shutdown_browser

        await shutdown_browser()
    except Exception:  # noqa: BLE001 — cleanup must never mask the verdict
        pass

    summary.finished_at = _utcnow_iso()
    loop_summary_path = _persist_loop_summary(
        summary, model=model, max_iters=max_iters, max_cost_usd=max_cost_usd
    )

    # Workflow version archive (operator decision 2026-06-11): EVERY loop end
    # persists the current workflow.py + assembled WORKFLOW_DOC.md + meta as an
    # immutable version under workspaces/<site>/workflow_versions/ (identical
    # bytes → a revalidation entry instead of a clone). Best-effort: archiving
    # must never affect the verdict.
    try:
        from runtime.workflow_registry import save_workflow_version

        workflow_version_dir = save_workflow_version(
            summary.site_id,
            verdict=summary.final_verdict,
            exit_reason=summary.exit_reason,
            iterations_run=len(summary.iterations),
            total_cost_usd=summary.total_cost_usd,
            model=model,
        )
    except Exception:  # noqa: BLE001
        workflow_version_dir = None

    if on_event:
        on_event(
            {
                "kind": "loop_done",
                "final_verdict": summary.final_verdict,
                "exit_reason": summary.exit_reason,
                "iterations_run": len(summary.iterations),
                "total_cost_usd": summary.total_cost_usd,
                "loop_summary_path": str(loop_summary_path) if loop_summary_path else None,
                "workflow_version": (
                    str(workflow_version_dir) if workflow_version_dir else None
                ),
            }
        )
    return summary


async def stream_explore_loop(
    site_id: str,
    **kwargs: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming wrapper — yields event dicts as the loop progresses,
    then a final LoopSummary dict."""
    events: list[dict[str, Any]] = []

    def _collect(ev: dict[str, Any]) -> None:
        events.append(ev)

    # Drain events as they accumulate while the loop runs concurrently.
    # We use a queue so the coroutine and the consumer can interleave.
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def _enqueue(ev: dict[str, Any]) -> None:
        queue.put_nowait(ev)

    async def _runner():
        result = await explore_until_pass(site_id, on_event=_enqueue, **kwargs)
        await queue.put({"kind": "_summary", "summary": result.to_dict()})
        await queue.put(None)

    task = asyncio.create_task(_runner())
    try:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
    finally:
        await task


__all__ = [
    "IterRecord",
    "LoopSummary",
    "explore_until_pass",
    "stream_explore_loop",
]
