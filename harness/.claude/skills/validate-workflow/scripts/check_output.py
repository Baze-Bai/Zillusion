#!/usr/bin/env python3
"""Validate a workspace's workflow.py against its goal.md.

Used by the validate-workflow skill via `!`bash`` injection. Prints a
human-readable report; exit code is informational only (the skill body
follows up regardless of exit code).

Usage:
    python check_output.py <site_id>

Reads:
    inputs/<site_id>/goal.md
    workspaces/<site_id>/workflow.py
    workspaces/<site_id>/output_sample.json

Reports:
    - whether output_sample.json exists and parses
    - record count
    - field coverage vs the fields the goal lists
    - whether workflow.py imports from helpers (a sanity-check)

Field extraction (heading_fields):
    Only scans bullet lines under a heading matching "Required fields",
    "Output fields", or "Output schema" (case-insensitive). Bullets under
    other sections (Scope, Constraints, Background, Framework-performance
    signals, etc.) are ignored — they often mention tool names / event
    names in backticks that look like fields but aren't.

Output shape (records):
    Top-level JSON array is the canonical shape. As a convenience, dict
    wrappers like {"rows": [...]} / {"records": [...]} / {"data": [...]} /
    {"items": [...]} / {"results": [...]} are auto-unwrapped before the
    is-it-a-list check. goal.md may pick either; check_output validates
    the unwrapped list either way.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


_FIELD_RE = re.compile(r"`([a-z_][a-z0-9_]*)`", re.IGNORECASE)
_FIELD_RE_BOLD = re.compile(r"\*\*([a-z_][a-z0-9_]*)\*\*", re.IGNORECASE)

# Tokens that match the field regex but are never actual data fields —
# they're language keywords / literal markers used inside field descriptions
# (``else `null` ``, ``defaults to `true` ``, etc). Lowercase comparison.
_FIELD_STOPWORDS = frozenset({
    "null", "none", "nil", "undefined",
    "true", "false",
    "n_a", "na",
    "int", "str", "bool", "float", "list", "dict", "array", "object",
    "todo", "tbd",
})

# A heading line that opens the "fields are listed below" section.
# Matches `## Required fields`, `### Output fields`, `# Output schema`, etc.
_TARGET_HEADING_RE = re.compile(
    r"^\s*#+\s*(required\s+fields?|output\s+fields?|output\s+schema)\b",
    re.IGNORECASE,
)
# Any markdown heading (we use it to detect "we left the fields section").
_ANY_HEADING_RE = re.compile(r"^\s*#+\s+\S")

# Top-level dict keys we auto-unwrap. Order matters only when multiple
# match (we pick the first present).
_UNWRAP_KEYS = ("rows", "records", "data", "items", "results")


def _clean_field_tokens(tokens) -> set[str]:
    """Drop stopwords (``null`` / ``true`` / type names / …) from a regex
    match set so they don't get reported as missing data fields."""
    return {t for t in tokens if t.lower() not in _FIELD_STOPWORDS}


def heading_fields(goal_md: str) -> set[str]:
    r"""Extract field names from goal.md.

    State machine: only collect ``\`field\``` / ``**field**`` tokens from
    bullet lines (``-`` / ``*`` at start of stripped line) that sit under
    a "Required fields" / "Output fields" / "Output schema" heading.
    Leaving the section (any other heading at any level) stops collection
    until the next matching heading.

    Without the section gate, bullets like "- Did `browser_player` get
    invoked?" in a framework-signals section get wrongly counted as
    required output fields.
    """
    fields: set[str] = set()
    in_target = False

    for line in goal_md.splitlines():
        if _TARGET_HEADING_RE.match(line):
            in_target = True
            continue
        # Any *other* heading exits the section.
        if _ANY_HEADING_RE.match(line) and not _TARGET_HEADING_RE.match(line):
            in_target = False
            continue
        if not in_target:
            continue
        stripped = line.lstrip()
        if not (stripped.startswith("-") or stripped.startswith("*")):
            continue
        fields.update(_clean_field_tokens(_FIELD_RE.findall(line)))
        fields.update(_clean_field_tokens(_FIELD_RE_BOLD.findall(line)))
    return fields


def output_fields(records: list) -> set[str]:
    fields: set[str] = set()
    for rec in records:
        if isinstance(rec, dict):
            fields.update(rec.keys())
    return fields


def _unwrap_records(raw):
    """Accept either a flat JSON array or a dict with a known wrapper key.

    Returns ``(records, unwrap_note)`` where ``unwrap_note`` is None for
    a top-level list, or a short string like ``"unwrapped 'rows'"`` when
    a wrapper was peeled off (so the caller can surface it). Returns
    ``(raw, None)`` unchanged when neither shape matches; the caller's
    is-it-a-list check then reports the type mismatch.
    """
    if isinstance(raw, list):
        return raw, None
    if isinstance(raw, dict):
        for key in _UNWRAP_KEYS:
            value = raw.get(key)
            if isinstance(value, list):
                return value, f"unwrapped {key!r} from dict"
    return raw, None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_output.py <site_id>", file=sys.stderr)
        print(
            "       (Skill caller: invoke as `Skill(skill=\"validate-workflow\", args=\"<site_id>\")`)",
            file=sys.stderr,
        )
        print(
            "       (Bash caller: `python .claude/skills/validate-workflow/scripts/check_output.py <site_id>`)",
            file=sys.stderr,
        )
        return 2
    site_id = argv[1]

    inputs_dir = Path("inputs") / site_id
    ws_dir = Path("workspaces") / site_id
    goal_path = inputs_dir / "goal.md"
    sample_path = ws_dir / "output_sample.json"
    workflow_path = ws_dir / "workflow.py"

    if not goal_path.exists():
        print(f"FAIL: {goal_path} not found")
        return 1
    if not workflow_path.exists():
        print(f"FAIL: {workflow_path} not found - write workflow.py first")
        return 1
    if not sample_path.exists():
        print(f"FAIL: {sample_path} not found - run `python workspaces/{site_id}/workflow.py`")
        return 1

    try:
        raw = json.loads(sample_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL: output_sample.json does not parse: {exc}")
        return 1

    records, unwrap_note = _unwrap_records(raw)
    if unwrap_note:
        print(f"INFO {unwrap_note}")

    if not isinstance(records, list):
        print(
            f"FAIL: output_sample.json is not a list (got {type(records).__name__}). "
            f"Top-level should be a JSON array, or a dict with one of "
            f"{list(_UNWRAP_KEYS)} as a list-valued key."
        )
        return 1

    print(f"OK   records: {len(records)}")
    if len(records) < 3:
        print(f"WARN records < 3 (expected at least 3)")

    goal_fields = heading_fields(goal_path.read_text(encoding="utf-8"))
    if goal_fields:
        present = output_fields(records)
        missing = goal_fields - present
        extra = present - goal_fields
        print(f"OK   goal lists {len(goal_fields)} fields")
        if missing:
            print(f"FAIL missing fields: {sorted(missing)}")
        else:
            print("OK   all goal fields present in output")
        if extra:
            print(f"INFO extra fields in output (not required): {sorted(extra)}")
    else:
        print(
            "WARN goal.md has no '## Required fields' / '## Output fields' / "
            "'## Output schema' section — field coverage not checked"
        )

    wf_text = workflow_path.read_text(encoding="utf-8")
    if "helpers" in wf_text:
        print("OK   workflow.py references helpers (good for auditability)")
    else:
        print("INFO workflow.py does not import helpers (fine if self-contained)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
