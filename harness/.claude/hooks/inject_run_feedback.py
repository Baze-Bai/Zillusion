#!/usr/bin/env python3
"""SessionStart hook: surface unresolved PRODUCTION-run outcomes.

Sister to `inject_validation_feedback.py` (which surfaces sample-VALIDATION
verdicts). This one scans every workspace for its LATEST full-crawl run —
`workspaces/<site>/runs/<run_id>/` — and surfaces the ones whose gate outcome
(`manifest.yaml`) is not a clean COMPLETE, or that carry Run-agent feedback.
Lists them in additionalContext so the next `/explore` picks up that the FULL
crawl hit a problem the sample validation could not (anti-bot at scale, deep
selector drift, low yield, mid-run crash).

Why this matters: the Run agent (self-contained SDK agent, see
`runtime/run_agent.py`) writes its outcome + feedback into an ISOLATED
`runs/<run_id>/` dir — it never touches the explore agent's `hypotheses.yaml`.
Without this hook the next `/explore` might not notice; with it, the agent sees
a one-line nudge at the top of context and reads `feedback.yaml` proactively.

`<run_id>` = the most-recently-modified subdir of `runs/`. The outcome is read
through `mcp_server.schemas.RunManifestFile` (single source of truth for the
gate); if the schema can't be imported here we fall back to the persisted
`outcome` key, which `save()` always writes.

Output is silent when every site's latest run is COMPLETE with no feedback (or
there are no runs yet).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import yaml

# Make the framework schema importable: this hook runs as a bare script, so
# sys.path[0] is .claude/hooks/, not the project root. parents[2] = root.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from mcp_server.schemas import RunManifestFile
except Exception:  # noqa: BLE001  (missing dep / import error — degrade gracefully)
    RunManifestFile = None  # type: ignore[assignment]

WORKSPACES_DIR = Path("workspaces")

# A run dir with NO manifest.yaml is either (a) a run in its first seconds
# (init_run_manifest lands almost immediately, and a live crawl touches the dir
# every ~1s) or (b) a session that CRASHED before initializing. Treat a
# manifest-less dir that has been QUIET this long as (b) and surface it.
_NO_MANIFEST_MIN_AGE_S = 600


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _latest_run_dir(runs_dir: Path) -> Path | None:
    """Most recent run subdir under workspaces/<site>/runs/ (by mtime)."""
    if not runs_dir.is_dir():
        return None
    runs = [d for d in runs_dir.iterdir() if d.is_dir()]
    if not runs:
        return None
    return max(runs, key=_mtime)


def _load_outcome(manifest_path: Path) -> str | None:
    """Gate outcome ('complete'|'partial'|'failed'|'aborted') for a manifest, or
    None. Prefer loading through RunManifestFile (the schema is the single source
    of truth for the gate); fall back to the persisted `outcome` key. Never
    raises: a SessionStart hook must not break session startup."""
    if not manifest_path.exists():
        return None
    if RunManifestFile is not None:
        try:
            return RunManifestFile.load(manifest_path).outcome
        except Exception:  # noqa: BLE001
            pass  # fall through to raw read
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict):
        v = data.get("outcome")
        if v in ("complete", "partial", "failed", "aborted"):
            return v
    return None


def _count_feedback(feedback_path: Path) -> int:
    if not feedback_path.exists():
        return 0
    try:
        data = yaml.safe_load(feedback_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    return len(data) if isinstance(data, list) else 0


def _scan_runs(site_dir: Path) -> dict | None:
    """Inspect a site's LATEST run. Returns None if there is no `runs/<run_id>/`
    with a readable outcome. Otherwise:
        {"site_id", "run_id", "outcome", "n_feedback", "run_rel"}."""
    run_dir = _latest_run_dir(site_dir / "runs")
    if run_dir is None:
        return None
    outcome = _load_outcome(run_dir / "manifest.yaml")
    if outcome is None:
        # No manifest: a just-started run (skip) or a pre-init crash (surface —
        # otherwise it is invisible AND masks any older run's feedback).
        if time.time() - _mtime(run_dir) < _NO_MANIFEST_MIN_AGE_S:
            return None
        outcome = "crashed"
    return {
        "site_id": site_dir.name,
        "run_id": run_dir.name,
        "outcome": outcome,
        "n_feedback": _count_feedback(run_dir / "feedback.yaml"),
        "run_rel": f"workspaces/{site_dir.name}/runs/{run_dir.name}",
    }


def _collect_pending() -> list[dict]:
    """Sites whose latest run is non-COMPLETE or carries feedback, sorted
    failed -> aborted -> partial -> complete, then by site id."""
    if not WORKSPACES_DIR.exists():
        return []
    out = []
    for site_dir in WORKSPACES_DIR.iterdir():
        if not site_dir.is_dir():
            continue
        info = _scan_runs(site_dir)
        if info and (info["outcome"] != "complete" or info["n_feedback"] > 0):
            out.append(info)
    severity = {"crashed": 0, "failed": 1, "aborted": 2, "partial": 3, "complete": 4}
    out.sort(key=lambda d: (severity.get(d["outcome"], 5), d["site_id"]))
    return out


def main() -> int:
    # Drain stdin per hook contract (the harness sends a JSON payload but we
    # don't need anything from it).
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass

    pending = _collect_pending()
    if not pending:
        return 0

    lines = [
        "## ⚠ Unresolved production-run outcomes",
        "",
        "The Run agent (`runtime.run_agent` / `runtime.cli run`) executed the "
        "validated workflow at FULL scope and the latest crawl is non-COMPLETE "
        "or left feedback on these workspaces:",
        "",
    ]
    for f in pending:
        fb = f"{f['n_feedback']} feedback item(s)" if f["n_feedback"] else "no feedback items"
        note = " — NO manifest (died before initializing?)" if f["outcome"] == "crashed" else ""
        lines.append(
            f"- **`{f['site_id']}`** — latest run **[{f['outcome'].upper()}]** "
            f"(run `{f['run_id']}`), {fb}{note}; see `{f['run_rel']}/`"
        )
    lines += [
        "",
        "If the user invokes `/explore <site_id>` for one of these sites, "
        "**read `<run_dir>/feedback.yaml` FIRST** — it holds the Run agent's "
        "feedback on what the FULL crawl revealed that sample-validation could "
        "not (scale anti-bot, deep selector drift, low yield, mid-run crash). "
        "If a run shows *no feedback items*, start from `<run_dir>/manifest.yaml` "
        "instead — each failing dimension records its `basis`/`evidence` (the "
        "gate that produced the outcome). Deeper diagnosis lives in the same "
        "dir: `report.md` (the Run agent's narrative), `snapshots/` "
        "(inspect_crawl_failure full-page screenshots + raw HTML, when taken), "
        "and `crawl_stdout.log` (the workflow's raw output). A **[CRASHED]** "
        "run left no manifest at all — the session died before initializing; "
        "its error trace is the latest `*-run.jsonl` under "
        "`workspaces/<site_id>/run-logs/` (crawl files exist only if the crawl "
        "had started). Fold what you "
        "learn into this run's own `hypotheses.yaml` and close the gaps. "
        "(`<run_dir>` = the run path shown above; the Run agent writes ONLY "
        "there, never to your `hypotheses.yaml`.)",
    ]

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "\n".join(lines),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
