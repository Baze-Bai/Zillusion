"""Pydantic-light schema for workspaces/<site>/iter_summary.md.

This file is mostly **markdown prose** — not a structured-record file —
because actor self-retrospection is an LLM-strong activity that doesn't
benefit from row-by-row schema enforcement (see
`memory/reference_cross_iter_memory_via_summary.md` for rationale).

What we DO enforce:

1. The file is **append-only** — each run's close adds a new `## Iter N`
   section, prior sections never get rewritten.
2. The latest section MUST start with `## Iter <N> summary` (regex match)
   so SessionStart hook can slice off the latest section cleanly.
3. Each section SHOULD contain three named sub-sections:
   - "## Tried this iter"
   - "## What didn't work (DO NOT retry next iter)"
   - "## Recommended next strategy"
   These are content rules; we don't fail if missing but the hook
   surfaces a warning ("iter N section missing DO NOT retry block").

So this module is a thin IO layer + soft content checks — not a Pydantic
RootModel because the content is prose.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


# Header regex: matches '## Iter 3 summary — reddit-trump' (or any tail)
ITER_HEADER_RE = re.compile(r"^## Iter (\d+)\s+summary\b", re.MULTILINE)

# Sub-section markers we expect (soft check)
EXPECTED_SUBSECTIONS = (
    "## Tried this iter",
    "## DO NOT retry",  # We accept "## What didn't work (DO NOT retry next iter)"
    "## Recommended next strategy",
)


class IterSummarySection(NamedTuple):
    """One ## Iter N section parsed out of iter_summary.md."""

    iter_n: int
    body: str  # full text of the section including the header line
    char_offset: int  # byte offset where this section starts in the file


def _iter_sections(text: str) -> list[IterSummarySection]:
    """Parse all ## Iter N sections in order they appear."""
    matches = list(ITER_HEADER_RE.finditer(text))
    if not matches:
        return []
    sections: list[IterSummarySection] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        iter_n = int(m.group(1))
        body = text[start:end].rstrip() + "\n"
        sections.append(IterSummarySection(iter_n=iter_n, body=body, char_offset=start))
    return sections


def load_latest_section(path: Path) -> IterSummarySection | None:
    """Return the most recent ## Iter N section or None if file missing/empty."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    sections = _iter_sections(text)
    if not sections:
        return None
    # Latest = highest iter_n (in case of out-of-order writes, though normally
    # the last one in the file IS the highest).
    return max(sections, key=lambda s: s.iter_n)


def next_iter_n(path: Path) -> int:
    """Compute the next iter number based on existing sections."""
    if not path.exists():
        return 1
    text = path.read_text(encoding="utf-8")
    sections = _iter_sections(text)
    if not sections:
        return 1
    return max(s.iter_n for s in sections) + 1


def check_required_subsections(section_body: str) -> list[str]:
    """Soft check: return list of expected subsection markers MISSING in body.

    Hook surfaces these as warnings; doesn't fail the load. Empty list = ok.
    """
    missing = []
    for marker in EXPECTED_SUBSECTIONS:
        # tolerate inexact wording — match on the marker text after '##'
        marker_text = marker.replace("## ", "").lower()
        if marker_text not in section_body.lower():
            missing.append(marker)
    return missing


def append_section(
    path: Path,
    *,
    site_id: str,
    iter_n: int | None = None,
    cost_usd: float | None = None,
    record_count: int | None = None,
    tried: list[str] | None = None,
    worked: list[str] | None = None,
    do_not_retry: list[str] | None = None,
    open_hypotheses: list[str] | None = None,
    next_strategy: str = "",
    free_text: str = "",
) -> int:
    """Append a new ## Iter N section to iter_summary.md.

    All structured kwargs are optional — caller can pass a fully free-form
    `free_text` if it has a custom structure. The structured kwargs render
    a known template that hook can rely on.

    Returns the iter_n actually written (auto-computed if None).
    """
    if iter_n is None:
        iter_n = next_iter_n(path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M UTC")

    parts: list[str] = []
    if not path.exists() or path.read_text(encoding="utf-8").strip() == "":
        # First write — add a file header so the file is self-describing.
        parts.append(
            "# Iter summary — managed by mcp_server.schemas.iter_summary\n"
            "# Append-only: each run's close adds a new `## Iter N` section.\n"
            "# Each section SHOULD contain Tried / DO NOT retry / Recommended next strategy subsections.\n"
            "# SessionStart hook injects the LATEST section into the next /explore run.\n"
        )

    header = f"\n## Iter {iter_n} summary — {site_id}"
    parts.append(header)
    meta_bits = [f"**Date**: {now}"]
    if cost_usd is not None:
        meta_bits.append(f"**Run cost**: ${cost_usd:.2f}")
    if record_count is not None:
        meta_bits.append(f"**Records**: {record_count}")
    parts.append(" | ".join(meta_bits))
    parts.append("")

    if tried:
        parts.append("## Tried this iter")
        parts.extend(f"- {t}" for t in tried)
        parts.append("")
    if worked:
        parts.append("## What worked")
        parts.extend(f"- {w}" for w in worked)
        parts.append("")
    if do_not_retry:
        parts.append("## What didn't work (DO NOT retry next iter)")
        parts.extend(f"- {d}" for d in do_not_retry)
        parts.append("")
    if open_hypotheses:
        parts.append("## Open going into next iter")
        parts.extend(f"- {h}" for h in open_hypotheses)
        parts.append("")
    if next_strategy:
        parts.append("## Recommended next strategy")
        parts.append(next_strategy.strip())
        parts.append("")
    if free_text:
        parts.append(free_text.strip())
        parts.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")
    return iter_n


__all__ = [
    "IterSummarySection",
    "load_latest_section",
    "next_iter_n",
    "check_required_subsections",
    "append_section",
    "EXPECTED_SUBSECTIONS",
]
