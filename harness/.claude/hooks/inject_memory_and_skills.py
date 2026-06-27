#!/usr/bin/env python3
"""SessionStart hook: surface cross-site index + per-workspace cross-iter signals.

Two layers of injection:

1. **Cross-site (always-on)**: memory/* + domain_skills/* indexes. Saves
   the agent from having to call memory_index / skill_list at the start
   of every run.

2. **Per-workspace (orchestrator-mode only)**: when env var
   `CRAWLER_EXPLORER_ACTIVE_SITE=<id>` is set (by explore_loop
   orchestrator), additionally inject the cross-iter learning signals
   for that site:
     - ⚑ pending operator steering: `user_steering.md` blocks newer than the
       agent's last `send_user_message` — surfaced as the CURRENT instruction
       (with a reply-first envelope), unlike the HISTORY conversation below
     - `task_plan.md` (the agent's living plan & expectations for the site)
     - `_last_run_status.json` (workflow success / bail / partial)
     - latest `## Iter N` section from `iter_summary.md`
     - latest isolated validation run: `validation/<run_id>/` verdict
       (scorecard.yaml) + validator feedback (feedback.yaml) + report tail
     - outstanding `hypotheses.yaml` items (unverified + high priority)
     - `workflow_history/` index + latest 2-snapshot diff (deterministic route)
     - agentic/inline: the prior harvest run's FAILED gate dimensions
       (runs/<run_id>/manifest.yaml), injected ACTIVELY (not just a pointer)

See `memory/reference_session_start_inject_pattern.md` for full design.

Caps (configurable constants below) keep total injected context to
~10-12KB so per-iter session-start cost stays bounded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Make the framework schema importable (hook runs as a bare script, so
# sys.path[0] is .claude/hooks/, not the project root). parents[2] = root.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from mcp_server.schemas import ScorecardFile
except Exception:  # noqa: BLE001
    ScorecardFile = None  # type: ignore[assignment]

# ─────────────────────────── config ──────────────────────────────

MEMORY_DIR = Path("memory")
SKILLS_DIR = Path("domain_skills")
WORKSPACES_DIR = Path("workspaces")

MAX_TASK_PLAN_CHARS = 6500  # holds the annotated per-field table + fields block
MAX_ITER_SUMMARY_CHARS = 2500
MAX_VALIDATION_REPORT_CHARS = 3000
MAX_WORKFLOW_DIFF_CHARS = 3000
OUTSTANDING_HYPOTHESES_LIMIT = 10
VALIDATION_FEEDBACK_LIMIT = 10

# ── Conversation-history (operator⇄agent) injection budget ───────────────────
# ⚠ PLACEHOLDER values — the history depth / truncation policy is OPERATOR-OWNED
# (Decision ②, not yet confirmed). These are conservative defaults that keep the
# section inside the hook's ~10-12KB total target; tune the numbers (or swap the
# most-recent-first strategy in conversation_history_lines) once the policy is set.
MAX_HISTORY_ENTRIES = 30  # keep the most-recent N exchanges verbatim
MAX_HISTORY_ENTRY_CHARS = 500  # per-entry clip
MAX_HISTORY_TOTAL_CHARS = 4000  # hard ceiling on the whole transcript block
PENDING_STEER_LIMIT = 3  # newest unacknowledged steers surfaced as PENDING (older stay in HISTORY)


# ─────────────────────────── cross-site (always-on) ──────────────


def memory_lines() -> list[str]:
    if not MEMORY_DIR.exists():
        return []
    entries = sorted(p for p in MEMORY_DIR.iterdir() if p.suffix == ".md")
    if not entries:
        return []
    out = ["## memory/ (cross-site notes available)"]
    for path in entries:
        text = path.read_text(encoding="utf-8", errors="replace")
        first = text.split("\n## [", 1)[0].strip()[:200]
        out.append(f"- `memory/{path.name}` ({len(text)} chars) - {first[:160]}")
    out.append("")
    out.append("Read with `memory_read` when a topic matches.")
    return out


def skill_lines() -> list[str]:
    if not SKILLS_DIR.exists():
        return []
    rows = []
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        meta = d / "metadata.yaml"
        if not meta.exists():
            continue
        try:
            data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        rows.append(
            {
                "skill_id": d.name,
                "title": str(data.get("title", d.name)),
                "when_to_use": str(data.get("when_to_use", "")).strip().splitlines()[0:1],
                "success_count": int(data.get("success_count", 0)),
            }
        )
    if not rows:
        return []
    out = ["## domain_skills/ (cross-site skill library)"]
    for r in rows:
        wt = r["when_to_use"][0] if r["when_to_use"] else ""
        out.append(
            f"- `{r['skill_id']}` (used {r['success_count']}x) - {r['title']}: {wt[:140]}"
        )
    out.append("")
    out.append("Read full record with `skill_read(skill_id)` when a skill applies.")
    return out


# ─────────────────────────── per-workspace (orchestrator-mode) ──────


def _slice_to_chars(text: str, max_chars: int) -> str:
    """Cap text, append truncation marker if cap bit."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n…(truncated; {len(text) - max_chars} chars omitted)"


def task_plan_lines(workspace_dir: Path) -> list[str]:
    """Inject task_plan.md — the agent's own plan & expectations for this
    site, carried across iters so the next run starts from the prior plan
    instead of re-deriving it from seed + goal. Freely mutable via
    workspace_write."""
    path = workspace_dir / "task_plan.md"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return []
    if not text:
        return []
    out = ["## Task plan & expectations (workspaces/<id>/task_plan.md, carried from prior iter)"]
    out.append("")
    out.append(_slice_to_chars(text, MAX_TASK_PLAN_CHARS))
    return out


def _parse_iso(s: object) -> "datetime | None":
    """Parse an ISO-8601 timestamp (with offset) into an aware datetime, or None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _iter_ledger(workspace_dir: Path) -> "list[tuple[datetime, int]]":
    """[(iter_start, iter_n)] ascending, from `_iter_ledger.jsonl` (written by the
    orchestrator at each iter start). Lets us (a) derive the iter for entries that
    carry no explicit iter stamp — operator-steering blocks, legacy messages — by
    timestamp window."""
    path = workspace_dir / "_iter_ledger.jsonl"
    if not path.exists():
        return []
    out: list[tuple[datetime, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_iso(rec.get("started_at"))
        it = rec.get("iter")
        if ts is not None and isinstance(it, int):
            out.append((ts, it))
    out.sort(key=lambda e: e[0])
    return out


def _iter_for_ts(ledger: "list[tuple[datetime, int]]", ts: "datetime | None") -> "int | None":
    """(a) The iter whose [start, next_start) window contains ts. None if no ledger
    / ts is None / ts precedes the first recorded iter."""
    if not ledger or ts is None:
        return None
    chosen: int | None = None
    for start, it in ledger:
        if ts >= start:
            chosen = it
        else:
            break
    return chosen


def _agent_message_entries(
    workspace_dir: Path,
) -> "list[tuple[datetime | None, int | None, str, str]]":
    """(ts, iter, 'AGENT→USER', text) from `_agent_messages.jsonl` (the messages
    the agent sent the user via send_user_message; `iter` present when stamped)."""
    path = workspace_dir / "_agent_messages.jsonl"
    if not path.exists():
        return []
    out: list[tuple[datetime | None, int | None, str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        text = (rec.get("message") or "").strip()
        if not text:
            continue
        it = rec.get("iter") if isinstance(rec.get("iter"), int) else None
        out.append((_parse_iso(rec.get("created_at")), it, "AGENT→USER", text))
    return out


def _operator_steer_entries(
    workspace_dir: Path,
) -> "list[tuple[datetime | None, int | None, str, str]]":
    """(ts, None, 'OPERATOR', text) parsed from `user_steering.md`, whose blocks
    the backend writes as `## Operator steering @ <iso-ts>` + body. Iter is left
    None here and derived from the ledger by time (a)."""
    path = workspace_dir / "user_steering.md"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    import re
    matches = list(re.finditer(r"^## Operator steering @ (.+?)\s*$", text, re.MULTILINE))
    out: list[tuple[datetime | None, int | None, str, str]] = []
    for i, m in enumerate(matches):
        ts = _parse_iso(m.group(1).strip())
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            out.append((ts, None, "OPERATOR", body))
    return out


def _resolved_conversation(
    workspace_dir: Path,
) -> "tuple[list[tuple[datetime | None, int | None, str, str]], datetime | None]":
    """Time-sorted operator⇄agent timeline with iters resolved — shared by the
    PENDING block and the HISTORY block. Returns (entries, last_agent_ts):
    entries ascending by time, last_agent_ts the newest parseable AGENT→USER
    timestamp (None when the agent has never messaged the user)."""
    entries = _agent_message_entries(workspace_dir) + _operator_steer_entries(workspace_dir)
    if not entries:
        return [], None
    ledger = _iter_ledger(workspace_dir)

    # Resolve iter: explicit write-time stamp (b) wins; else derive by time (a).
    resolved: list[tuple[datetime | None, int | None, str, str]] = [
        (ts, it if it is not None else _iter_for_ts(ledger, ts), role, body)
        for ts, it, role, body in entries
    ]

    # Chronological; entries with no parseable ts sort last (treat as newest).
    _far_future = datetime.max.replace(tzinfo=timezone.utc)
    resolved.sort(key=lambda e: e[0] or _far_future)
    agent_ts = [ts for ts, _it, role, _b in resolved if role == "AGENT→USER" and ts is not None]
    return resolved, (max(agent_ts) if agent_ts else None)


def _pending_steers(
    resolved: "list[tuple[datetime | None, int | None, str, str]]",
    last_agent_ts: "datetime | None",
) -> "list[tuple[datetime | None, int | None, str, str]]":
    """OPERATOR entries the agent has not yet replied to: timestamp after the
    agent's last user-message (all of them when the agent never messaged).
    ts-less entries are excluded — they can't be ordered against the replies."""
    return [
        e
        for e in resolved
        if e[2] == "OPERATOR"
        and e[0] is not None
        and (last_agent_ts is None or e[0] > last_agent_ts)
    ]


def pending_steering_lines(workspace_dir: Path) -> list[str]:
    """Surface unacknowledged operator steers as the injection's HEADLINE — the
    operator's CURRENT instructions, deliberately split out of the HISTORY block
    below. The envelope mirrors the live mid-run ⚑ header (explore_loop's
    _tail_user_steering): honor + reply FIRST via send_user_message. Newest
    PENDING_STEER_LIMIT shown; older unacknowledged ones stay in HISTORY. An
    entry leaves this block as soon as the agent sends ANY user message after
    it (mechanical, timeline-based — replying once clears the backlog)."""
    resolved, last_agent_ts = _resolved_conversation(workspace_dir)
    pending = _pending_steers(resolved, last_agent_ts)
    if not pending:
        return []
    shown = pending[-PENDING_STEER_LIMIT:]
    older = len(pending) - len(shown)
    out = [
        "## ⚑ PENDING OPERATOR STEERING — unacknowledged; CURRENT instruction, NOT history",
        "",
        "The operator sent these and you have NOT yet replied. They are the "
        "operator's live instructions (precedence rank 2, despite arriving via "
        "injection — NOT background): reconcile your task_plan.md and current "
        "plan with them, then continue. Reply FIRST with send_user_message — "
        "acknowledge what they asked AND say concretely how you're adjusting "
        "(or why you won't); a bare \"OK\" doesn't count; fold ALL of them into "
        "ONE reply. Listed oldest→newest; when they conflict, the newest wins — "
        "say so in your reply.",
        "",
    ]
    for ts, it, _role, body in shown:
        tstr = ts.isoformat(timespec="seconds").replace("+00:00", "Z") if ts else "??"
        itag = f"iter{it}" if it is not None else "iter?"
        out.append(f"- [{itag} · {tstr}] {_slice_to_chars(body, MAX_HISTORY_ENTRY_CHARS)}")
    if older:
        out.append("")
        out.append(f"_({older} older unacknowledged steer(s) remain in HISTORY below.)_")
    return out


def conversation_history_lines(workspace_dir: Path) -> list[str]:
    """Inject the operator⇄agent conversation as HISTORY — 5th-channel background,
    explicitly NOT a current instruction. Operator steering (user_steering.md) is
    interleaved with the agent's own messages to the user (_agent_messages.jsonl),
    ordered by time, each tagged `[iterN · time · HISTORY]`. Steers surfaced in
    the PENDING block above are omitted here (no double-delivery); the live
    instruction for THIS iter arrives separately (current prompt / live steer
    turns); standing intent is carried by task_plan.md. Budget-capped
    most-recent-first."""
    resolved, last_agent_ts = _resolved_conversation(workspace_dir)
    if not resolved:
        return []
    shown_pending = _pending_steers(resolved, last_agent_ts)[-PENDING_STEER_LIMIT:]
    pending_keys = {(ts, role, body) for ts, _it, role, body in shown_pending}
    resolved = [e for e in resolved if (e[0], e[2], e[3]) not in pending_keys]
    if not resolved:
        return []

    omitted = 0
    if len(resolved) > MAX_HISTORY_ENTRIES:
        omitted = len(resolved) - MAX_HISTORY_ENTRIES
        resolved = resolved[-MAX_HISTORY_ENTRIES:]

    out = [
        "## Operator⇄agent conversation — HISTORY (background record, NOT this iter's instruction)",
        "",
        "Past operator guidance and your past messages to the user, for context only. "
        "These are HISTORICAL and may be superseded — do NOT treat them as the current "
        "instruction. The live task is the current prompt; standing intent lives in "
        "task_plan.md.",
        "",
    ]
    if pending_keys:
        out.append(
            f"_({len(pending_keys)} unacknowledged steer(s) surfaced in the "
            "PENDING block above, omitted here.)_"
        )
        out.append("")
    if omitted:
        out.append(f"_({omitted} older exchange(s) omitted by budget.)_")
        out.append("")
    for ts, it, role, body in resolved:
        tstr = ts.isoformat(timespec="seconds").replace("+00:00", "Z") if ts else "??"
        itag = f"iter{it}" if it is not None else "iter?"
        out.append(
            f"- [{itag} · {tstr} · HISTORY] **{role}:** "
            f"{_slice_to_chars(body, MAX_HISTORY_ENTRY_CHARS)}"
        )

    # Hard total-size ceiling (covers pathological entry counts/sizes).
    block = "\n".join(out)
    if len(block) > MAX_HISTORY_TOTAL_CHARS:
        block = (
            block[:MAX_HISTORY_TOTAL_CHARS].rstrip() + "\n\n…(history truncated by total budget)"
        )
        return block.split("\n")
    return out


def last_run_status_lines(workspace_dir: Path) -> list[str]:
    """Inject `_last_run_status.json` so agent knows if last workflow run
    succeeded / bailed / wrote partial — avoids stale-output misreads."""
    path = workspace_dir / "_last_run_status.json"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return []
    out = ["## Last workflow run status"]
    out.append("```json")
    out.append(json.dumps(data, indent=2, ensure_ascii=False))
    out.append("```")
    if data.get("status") == "bailed":
        out.append("")
        out.append(
            "**⚠ Last run BAILED.** `output_sample.json` reflects no data this run "
            "(was truncated to `[]`). Don't read it as fresh extraction."
        )
    return out


def iter_summary_latest_lines(workspace_dir: Path) -> list[str]:
    """Inject the LATEST `## Iter N` block from iter_summary.md.

    This is the primary memory-carrier across iters — the previous
    actor's self-retrospection including their DO NOT retry list.
    """
    path = workspace_dir / "iter_summary.md"
    if not path.exists():
        return []
    # Inline the load to avoid importing mcp_server (hook should be self-contained)
    text = path.read_text(encoding="utf-8")
    import re
    matches = list(re.finditer(r"^## Iter (\d+)\s+summary\b", text, re.MULTILINE))
    if not matches:
        return []
    last = max(matches, key=lambda m: int(m.group(1)))
    start = last.start()
    # Find next ## Iter or end of file
    next_iter = [m for m in matches if m.start() > start]
    end = next_iter[0].start() if next_iter else len(text)
    section = text[start:end].rstrip()
    out = ["## Last iter summary (workspaces/<id>/iter_summary.md, latest section)"]
    out.append("")
    out.append(_slice_to_chars(section, MAX_ITER_SUMMARY_CHARS))
    return out


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _latest_run_dir(validation_dir: Path) -> Path | None:
    """Most recent run subdir under workspaces/<site>/validation/ (by mtime)."""
    if not validation_dir.is_dir():
        return None
    runs = [d for d in validation_dir.iterdir() if d.is_dir()]
    if not runs:
        return None
    return max(runs, key=_mtime)


def _load_overall(scorecard_path: Path) -> str | None:
    """Gate verdict ('pass'|'fail'|'inconclusive') for a scorecard, or None.

    Prefer loading through ScorecardFile (single source of truth for the gate);
    fall back to the persisted `overall` key (which `save()` always writes) if
    the schema can't be imported / loaded here. Never raises.
    """
    if not scorecard_path.exists():
        return None
    if ScorecardFile is not None:
        try:
            return ScorecardFile.load(scorecard_path).overall
        except Exception:  # noqa: BLE001
            pass
    try:
        data = yaml.safe_load(scorecard_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict):
        v = data.get("overall")
        if v in ("pass", "fail", "inconclusive"):
            return v
    return None


def _feedback_items(feedback_path: Path) -> list[dict]:
    if not feedback_path.exists():
        return []
    try:
        data = yaml.safe_load(feedback_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []


def validation_latest_lines(workspace_dir: Path) -> list[str]:
    """Inject the LATEST isolated validation run's signals: verdict
    (scorecard.yaml) + validator feedback (feedback.yaml) + report tail.

    Replaces the old `validation_report.md` scan — the validator now writes
    into an isolated `validation/<run_id>/` dir and never touches the explore
    agent's `hypotheses.yaml`. The next iter reads `feedback.yaml` and folds
    those items into its own hypotheses. `<run_id>` = newest subdir by mtime.
    """
    run_dir = _latest_run_dir(workspace_dir / "validation")
    if run_dir is None:
        return []
    rel = f"validation/{run_dir.name}"
    overall = _load_overall(run_dir / "scorecard.yaml")
    items = _feedback_items(run_dir / "feedback.yaml")

    report_text = ""
    report = run_dir / "report.md"
    if report.exists():
        try:
            report_text = report.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            report_text = ""

    if overall is None and not items and not report_text:
        return []  # run dir exists but nothing readable yet

    header = f"## Last validation run ({rel})"
    if overall:
        header += f" — verdict **[{overall.upper()}]**"
    out = [header, ""]

    if items:
        out.append(f"### Validator feedback ({rel}/feedback.yaml) — {len(items)} item(s)")
        for it in items[:VALIDATION_FEEDBACK_LIMIT]:
            claim = (it.get("claim") or "").strip().split("\n")[0][:160]
            pri = str(it.get("priority") or "medium")
            out.append(f"- [{pri}] {claim}")
            notes = (it.get("notes") or "").strip()
            if notes:
                out.append(f"  - notes: {notes[:120]}")
        out.append("")
        out.append("Fold these into this run's `hypotheses.yaml` and close the gaps.")

    if report_text:
        out.append("")
        out.append(f"### Validator report ({rel}/report.md)")
        out.append("")
        out.append(_slice_to_chars(report_text, MAX_VALIDATION_REPORT_CHARS))

    return out


def outstanding_hypotheses_lines(workspace_dir: Path) -> list[str]:
    """Inject hypotheses.yaml entries with status=unverified + priority=high."""
    path = workspace_dir / "hypotheses.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return ["## Outstanding hypotheses",
                "(hypotheses.yaml unparseable — check file structure)"]
    if not isinstance(data, list):
        return ["## Outstanding hypotheses",
                f"(hypotheses.yaml has wrong top-level shape: {type(data).__name__}; expected list)"]
    outstanding = [
        h for h in data
        if isinstance(h, dict)
        and h.get("status") == "unverified"
        and h.get("priority") == "high"
    ]
    if not outstanding:
        return []
    outstanding = outstanding[:OUTSTANDING_HYPOTHESES_LIMIT]
    out = [f"## Outstanding hypotheses (status=unverified, priority=high) — {len(outstanding)} items"]
    for h in outstanding:
        hid = h.get("id", "H???")
        claim = (h.get("claim") or "").strip().split("\n")[0][:140]
        source = (h.get("source") or "").strip()[:80]
        out.append(f"- **{hid}**: {claim}")
        if source:
            out.append(f"  - source: {source}")
    return out


def workflow_history_lines(workspace_dir: Path) -> list[str]:
    """Inject workflow_history/ file index + latest 2-snapshot diff."""
    history_dir = workspace_dir / "workflow_history"
    if not history_dir.exists():
        return []
    snapshots = sorted(history_dir.glob("iter_*.py"))
    if not snapshots:
        return []
    out = [f"## Workflow snapshots history ({len(snapshots)} saved)"]
    for s in snapshots:
        try:
            size_kb = s.stat().st_size / 1024
            out.append(f"- `workflow_history/{s.name}`  ({size_kb:.1f} KB)")
        except OSError:
            out.append(f"- `workflow_history/{s.name}`  (size unknown)")

    # Latest diff: prior snapshot vs current workflow.py
    current = workspace_dir / "workflow.py"
    if not current.exists():
        return out
    prev = snapshots[-1]
    try:
        completed = subprocess.run(
            ["git", "diff", "--no-index", str(prev), str(current)],
            capture_output=True, text=True, timeout=10,
        )
        # git diff exits 1 when files differ (not an error for us)
        diff = completed.stdout or ""
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return out  # git not available; skip diff section gracefully

    if not diff.strip():
        out.append("")
        out.append("(No changes since last snapshot.)")
        return out

    diff = _slice_to_chars(diff, MAX_WORKFLOW_DIFF_CHARS)
    out.append("")
    out.append(f"## Last workflow.py changes (prev snapshot `{prev.name}` → current `workflow.py`)")
    out.append("```diff")
    out.append(diff)
    out.append("```")
    out.append("")
    out.append(
        "For older diffs, use Bash: "
        "`git diff workflow_history/iter_AA_*.py workflow_history/iter_BB_*.py`"
    )
    return out


def _task_plan_declared_fields(workspace_dir: Path) -> set[str] | None:
    """Field names from task_plan.md's machine-readable ```fields block
    (the convention defined in /explore step 4). None when the file or the
    block is absent — older plans without the block are skipped, never
    flagged."""
    path = workspace_dir / "task_plan.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    import re
    m = re.search(r"```[ \t]*fields\b[ \t]*\r?\n(.*?)```", text, re.DOTALL)
    if not m:
        return None
    fields: set[str] = set()
    for line in m.group(1).splitlines():
        tok = line.strip().lstrip("-").strip().strip("`").strip()
        if not tok or tok.startswith("#"):
            continue
        fields.add(tok.split()[0].strip("`"))
    return fields or None


def _selectors_declared_fields(workspace_dir: Path) -> set[str] | None:
    """Canonical field names = keys of selectors.yaml `selectors.fields`.
    Plain yaml load (no schema dependency, so a malformed selectors block
    can't break the hook). None if absent/unparseable."""
    path = workspace_dir / "selectors.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    sel = data.get("selectors") if isinstance(data, dict) else None
    fields = sel.get("fields") if isinstance(sel, dict) else None
    if not isinstance(fields, dict):
        return None
    return set(fields.keys()) or None


def task_plan_divergence_lines(workspace_dir: Path) -> list[str]:
    """Deterministic backstop for the step-9 reconciliation: diff
    task_plan's declared fields against selectors.yaml's confirmed field
    keys and surface any mismatch so this iter fixes it. Non-blocking hint,
    NOT a gate. Silent when they agree, when either side is missing, or when
    task_plan has no ```fields block (nothing to diff)."""
    declared = _task_plan_declared_fields(workspace_dir)
    confirmed = _selectors_declared_fields(workspace_dir)
    if declared is None or confirmed is None:
        return []
    only_plan = sorted(declared - confirmed)
    only_sel = sorted(confirmed - declared)
    if not only_plan and not only_sel:
        return []
    out = ["## ⚠ task_plan ↔ selectors.yaml field divergence (deterministic check)"]
    if only_plan:
        out.append("- task_plan declares, selectors.yaml lacks: "
                   + ", ".join(f"`{f}`" for f in only_plan))
    if only_sel:
        out.append("- selectors.yaml confirms, task_plan omits: "
                   + ", ".join(f"`{f}`" for f in only_sel))
    out.append("")
    out.append(
        'Reconcile task_plan.md\'s "Target data & fields" (incl. its '
        "`fields` block) with the confirmed `selectors.fields` keys "
        "(/explore step 9). Fix ONLY these mismatches — drop what proved "
        "unextractable, add what you discovered, leave the rest."
    )
    return out


def _declared_route(workspace_dir: Path) -> "str | None":
    """The terminal route declared for this site (crawl_route.json), or None."""
    p = workspace_dir / "crawl_route.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("route") if isinstance(data, dict) else None


def _latest_crawl_run(workspace_dir: Path) -> "Path | None":
    """Most-recently-modified runs/<run_id>/ dir (the prior iter's harvest)."""
    runs = workspace_dir / "runs"
    if not runs.is_dir():
        return None
    subs = [d for d in runs.iterdir() if d.is_dir()]
    return max(subs, key=_mtime) if subs else None


def run_gate_failures_lines(workspace_dir: Path) -> list[str]:
    """AGENTIC / INLINE iter-level scaffold — the analog of the deterministic
    workflow.py-diff + task_plan↔selectors scaffolds. When the prior iter's
    harvest run did NOT gate-compute COMPLETE, ACTIVELY inject its failing GATING
    dimensions + their basis, so the next iter sees WHAT to fix without first
    having to Read manifest.yaml (that passive pointer is inject_run_feedback's
    job). Silent for deterministic (covered by the workflow / validator
    scaffolds) and when the latest run is a clean COMPLETE."""
    route = _declared_route(workspace_dir)
    if route not in ("agentic", "inline"):
        return []
    run_dir = _latest_crawl_run(workspace_dir)
    if run_dir is None:
        return []
    try:
        data = yaml.safe_load((run_dir / "manifest.yaml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict):
        return []
    outcome = str(data.get("outcome") or "").lower()
    if outcome == "complete":
        return []
    dims = data.get("dimensions")
    if not isinstance(dims, dict):
        return []
    failed = [
        (name, d.get("status"), (d.get("basis") or d.get("evidence") or "").strip())
        for name, d in dims.items()
        if isinstance(d, dict)
        and d.get("gating")
        and d.get("status") in ("fail", "not_verified")
    ]
    if not failed:
        return []
    label = outcome.upper() or "non-COMPLETE"
    out = [
        f"## ⚠ Last {route} run did not COMPLETE (outcome={label}, run `{run_dir.name}`)",
        f"The previous iteration's {route} harvest gate-computed **{label}**. These "
        "GATING checks are not passing — address them this iter, or pivot the route:",
    ]
    for name, status, basis in failed:
        line = f"- **{name}** ({status})"
        if basis:
            line += f": {_slice_to_chars(basis, 300)}"
        out.append(line)
    return out


def per_workspace_sections(site_id: str) -> list[str]:
    """Compose all per-workspace sections in priority order."""
    workspace_dir = WORKSPACES_DIR / site_id
    if not workspace_dir.exists():
        return []
    # ⚑ Pending (unacknowledged) operator steering is the HEADLINE — it is the
    # operator's current instruction, not background. task_plan (the active
    # plan) follows; the acknowledged operator⇄agent conversation is
    # HISTORY/background, so it sits low in the tail next to the other
    # retrospective context.
    sections: list[list[str]] = [
        pending_steering_lines(workspace_dir),
        task_plan_lines(workspace_dir),
        task_plan_divergence_lines(workspace_dir),
        run_gate_failures_lines(workspace_dir),
        last_run_status_lines(workspace_dir),
        iter_summary_latest_lines(workspace_dir),
        validation_latest_lines(workspace_dir),
        outstanding_hypotheses_lines(workspace_dir),
        conversation_history_lines(workspace_dir),
        workflow_history_lines(workspace_dir),
    ]
    out: list[str] = []
    for sec in sections:
        if sec:
            out.extend(sec)
            out.append("")
    return out


# ─────────────────────────── main ───────────────────────────────


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass

    sections: list[str] = []

    # 1. Cross-site (always-on)
    mem = memory_lines()
    if mem:
        sections.extend(mem)
        sections.append("")
    sk = skill_lines()
    if sk:
        sections.extend(sk)
        sections.append("")

    # 2. Per-workspace (orchestrator-mode: env var must be set)
    active_site = os.environ.get("CRAWLER_EXPLORER_ACTIVE_SITE", "").strip()
    if active_site:
        ws_sections = per_workspace_sections(active_site)
        if ws_sections:
            sections.append(f"---  PER-WORKSPACE CROSS-ITER CONTEXT (site={active_site})  ---")
            sections.append("")
            sections.extend(ws_sections)

    if not sections:
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "\n".join(sections),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
