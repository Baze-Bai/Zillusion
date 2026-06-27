#!/usr/bin/env python3
"""SessionStart hook: surface unresolved validation verdicts.

Sister to `inject_memory_and_skills.py`. Scans every workspace for its
LATEST isolated validation run — `workspaces/<site>/validation/<run_id>/`
— and surfaces the ones whose gate verdict (`scorecard.yaml`) is not a
clean PASS, or that carry validator feedback. Lists them in
additionalContext so the next `/explore` picks up that there's upstream
feedback to address.

Why this matters: the validator (self-contained SDK agent, see
`runtime/validate.py`) writes its verdict + feedback into an ISOLATED
`validation/<run_id>/` dir — it never touches the explore agent's
`hypotheses.yaml`. Without this hook the next `/explore` agent might
not notice that dir; with it, the agent sees a one-line nudge at the
very top of context and reads `feedback.yaml` proactively.

`<run_id>` = the most-recently-modified subdir of `validation/`. The
verdict is read through `mcp_server.schemas.ScorecardFile` (single
source of truth for the gate); if the schema can't be imported here we
fall back to the persisted `overall` key, which `save()` always writes.

The site actively being iterated has the newest validation run across all
workspaces, so its feedback items are INLINED verbatim (high priority first,
within `_INLINE_BUDGET`) at the top of context — the agent shouldn't have to
choose to Read the file, and inlined-at-top content stays salient through the
~80 turns until it writes selectors.yaml. Other pending sites stay one-line
pointers. The full `feedback.yaml` / `report.md` are always one Read away.

Output is silent when every site's latest verdict is PASS with no
feedback (or there are no validation runs yet).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

# Make the framework schema importable: this hook runs as a bare script, so
# sys.path[0] is .claude/hooks/, not the project root. parents[2] = root.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from mcp_server.schemas import ScorecardFile
except Exception:  # noqa: BLE001  (missing dep / import error — degrade gracefully)
    ScorecardFile = None  # type: ignore[assignment]

WORKSPACES_DIR = Path("workspaces")


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

    Prefer loading through ScorecardFile (the schema is the single source of
    truth for the gate). Fall back to the persisted `overall` key if the
    schema can't be imported / loaded here — `save()` always writes it, so the
    persisted value matches the gate for any well-formed file. Never raises:
    a SessionStart hook must not break session startup.
    """
    if not scorecard_path.exists():
        return None
    if ScorecardFile is not None:
        try:
            return ScorecardFile.load(scorecard_path).overall
        except Exception:  # noqa: BLE001
            pass  # fall through to raw read
    try:
        data = yaml.safe_load(scorecard_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict):
        v = data.get("overall")
        if v in ("pass", "fail", "inconclusive"):
            return v
    return None


def _load_feedback_items(feedback_path: Path) -> list[dict]:
    """Parsed feedback items (list of {claim, basis, priority, notes}), or [].

    Never raises — a SessionStart hook must not break session startup.
    """
    if not feedback_path.exists():
        return []
    try:
        data = yaml.safe_load(feedback_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


# Cap on inlined feedback so a pathological feedback.yaml can't dominate the
# explore agent's opening context — the full file is always one Read away.
_INLINE_BUDGET = 3500
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _render_items(items: list[dict]) -> list[str]:
    """Render feedback items verbatim — claim + basis, high priority first —
    within `_INLINE_BUDGET` chars. Returns markdown lines and notes a
    truncation pointer if the budget is hit mid-list."""
    ordered = sorted(
        items,
        key=lambda it: _PRIORITY_ORDER.get(str(it.get("priority", "")).lower(), 3),
    )
    lines: list[str] = []
    used = 0
    shown = 0
    for it in ordered:
        prio = str(it.get("priority", "?")).upper()
        claim = str(it.get("claim", "")).strip()
        basis = str(it.get("basis", "")).strip()
        block = f"- **[{prio}]** {claim}"
        if basis:
            block += f"\n  - _basis:_ {basis}"
        if shown and used + len(block) > _INLINE_BUDGET:
            lines.append(
                f"- _(+{len(ordered) - shown} more feedback item(s) — read the full "
                f"`feedback.yaml`)_"
            )
            break
        lines.append(block)
        used += len(block)
        shown += 1
    return lines


def _scan_validation(site_dir: Path) -> dict | None:
    """Inspect a site's LATEST isolated validation run.

    Returns None if there is no `validation/<run_id>/` with a readable
    verdict. Otherwise returns
        {"site_id", "run_id", "overall", "n_feedback", "items", "mtime", "run_rel"}.
    """
    run_dir = _latest_run_dir(site_dir / "validation")
    if run_dir is None:
        return None
    overall = _load_overall(run_dir / "scorecard.yaml")
    if overall is None:
        return None
    items = _load_feedback_items(run_dir / "feedback.yaml")
    return {
        "site_id": site_dir.name,
        "run_id": run_dir.name,
        "overall": overall,
        "n_feedback": len(items),
        "items": items,
        "mtime": _mtime(run_dir),
        "run_rel": f"workspaces/{site_dir.name}/validation/{run_dir.name}",
    }


def _collect_pending() -> list[dict]:
    """Sites whose latest validation run is non-PASS or carries feedback,
    sorted fail -> inconclusive -> pass, then by site id."""
    if not WORKSPACES_DIR.exists():
        return []
    out = []
    for site_dir in WORKSPACES_DIR.iterdir():
        if not site_dir.is_dir():
            continue
        info = _scan_validation(site_dir)
        if info and (info["overall"] != "pass" or info["n_feedback"] > 0):
            out.append(info)
    severity = {"fail": 0, "inconclusive": 1, "pass": 2}
    out.sort(key=lambda d: (severity.get(d["overall"], 3), d["site_id"]))
    return out


def main() -> int:
    # Drain stdin per hook contract (the harness sends a JSON payload but
    # we don't need anything from it).
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass

    pending = _collect_pending()
    if not pending:
        return 0

    # The site actively being iterated has the most-recently-modified validation
    # run (the loop validates → re-explores the same site). Inline ITS feedback
    # verbatim — cheap (~1.5K chars) and in the highest-attention position — so
    # the agent can't miss the exact asks instead of relying on it choosing to
    # Read the file 80 turns before it writes selectors.yaml. Other pending sites
    # stay one-line pointers.
    primary = max(pending, key=lambda d: d["mtime"])
    others = [f for f in pending if f["site_id"] != primary["site_id"]]

    lines = [
        "## ⚠ Unresolved validation verdicts",
        "",
        f"Latest validation for **`{primary['site_id']}`** is "
        f"**[{primary['overall'].upper()}]** (run `{primary['run_id']}`). The "
        f"validator's feedback for your next `/explore` pass — fold each into this "
        f"run's `hypotheses.yaml` / `selectors.yaml` and close it:",
        "",
    ]
    if primary["items"]:
        lines += _render_items(primary["items"])
    else:
        lines.append("- _(no structured feedback items — skim the report below)_")
    lines += [
        "",
        f"Full detail in `{primary['run_rel']}/feedback.yaml` (+ `report.md` for the "
        f"narrative). A **gating-dim** claim is NOT advisory — close it; do not wave "
        f"it through as a 'structural limitation' and declare DONE. The validator "
        f"writes ONLY under that path, never to your `hypotheses.yaml`.",
    ]

    if others:
        lines += ["", "Other workspaces with open verdicts/feedback:"]
        for f in others:
            fb = f"{f['n_feedback']} feedback item(s)" if f["n_feedback"] else "no feedback items"
            lines.append(
                f"- **`{f['site_id']}`** — **[{f['overall'].upper()}]** "
                f"(run `{f['run_id']}`), {fb}; see `{f['run_rel']}/` — read its "
                f"`feedback.yaml` FIRST if you `/explore` it."
            )

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
