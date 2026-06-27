"""Pydantic schema for workspaces/<site>/crawl_brief.md — the Explore→Agentic-Crawl
handoff.

When Explore judges a site's control flow too dynamic for a deterministic
workflow.py, it picks the AGENTIC route and writes this brief: the "battle plan"
the persistent crawl agent reads to harvest the data. It is structured (validated
here) but rendered to markdown the agent reads with workspace_read.

The load-bearing field is the **completeness anchor**: it is MANDATORY (a brief
without one is rejected) and carries an explicit **hardness** label — because the
crawl agent judges "done" against it, and the operator decided completion is the
agent's own judgment with no token cap. The anchor's hardness tells the agent
(and a watching operator) how trustworthy that judgment is:

  - ``hard``  — an enumerable index (a total count, a category tree, a date
    window): "done" is objectively verifiable (cursor reached the full set).
    ``estimated_total`` SHOULD be set so progress is a precise ratio.
  - ``soft``  — only a stop heuristic (infinite scroll with no total): "done" is
    the agent's subjective call, justified against the heuristic.

Explore must label hardness HONESTLY — a soft anchor is a flag that completeness
is harder to govern (higher spend risk), surfaced up front rather than discovered
mid-crawl.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator


class CompletenessAnchor(BaseModel):
    """How the crawl agent knows when the harvest is complete."""

    hardness: Literal["hard", "soft"]
    full_set: str  # what the full set is, in this site's terms
    enumeration: str  # how to walk it (pagination, category tree, date range)
    termination: str  # the concrete signal that you've covered it
    estimated_total: int | None = None  # full-set size (hard anchors → precise ratio)
    progress_metric: str | None = None  # how progress is measured

    model_config = {"extra": "forbid"}

    @field_validator("full_set", "enumeration", "termination")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError(
                "completeness anchor needs full_set, enumeration, and termination — "
                "an anchor the agent can actually judge 'done' against"
            )
        return v


class CrawlBriefFile(BaseModel):
    """Top-level shape of workspaces/<site>/crawl_brief.md (validated on write)."""

    site_id: str
    goal_summary: str  # what data to harvest, one record per what
    field_schema: dict[str, str] = {}  # field name -> semantic / type note
    identifier_field: str | None = None  # dedup key (passed to init_crawl)
    completeness_anchor: CompletenessAnchor  # MANDATORY
    why_agentic: str  # the dynamics that defeat a static workflow.py
    proven_knowledge: str = ""  # helpers / selectors / endpoints / auth (pointers)
    hazards: str = ""  # walls, safe cadence, anti-bot
    recommended_strategy: str = ""  # how to batch, where takeover/per-unit may be needed

    model_config = {"extra": "forbid"}

    @field_validator("goal_summary", "why_agentic")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("goal_summary and why_agentic are required")
        return v

    # ── render to the markdown the crawl agent reads ─────────────────
    def to_markdown(self) -> str:
        a = self.completeness_anchor
        fields = (
            "\n".join(f"- `{n}` — {s}" for n, s in self.field_schema.items())
            if self.field_schema
            else "(see goal.md / selectors.yaml)"
        )
        idf = (
            f"\n- **identifier_field**: `{self.identifier_field}`" if self.identifier_field else ""
        )
        est = (
            f"\n- **estimated_total: {a.estimated_total}**" if a.estimated_total is not None else ""
        )
        prog = f"\n- progress_metric: {a.progress_metric}" if a.progress_metric else ""
        return f"""\
# Crawl Brief — {self.site_id}

## Goal + field schema
{self.goal_summary}
{fields}{idf}

## Completeness anchor — {a.hardness.upper()}
- **hardness: {a.hardness}**{est}
- full_set: {a.full_set}
- enumeration: {a.enumeration}
- termination: {a.termination}{prog}

## Why agentic
{self.why_agentic}

## Proven knowledge
{self.proven_knowledge or "(none recorded — fall back to helpers.py / selectors.yaml)"}

## Hazards / cadence
{self.hazards or "(none recorded)"}

## Recommended strategy
{self.recommended_strategy or "(none recorded — you decide batching)"}
"""

    def save_markdown(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_markdown(), encoding="utf-8")
        tmp.replace(path)

    def save_yaml(self, path: Path) -> None:
        """Structured copy (debug / orchestrator), alongside the markdown."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(self.model_dump(), allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        tmp.replace(path)


__all__ = ["CrawlBriefFile", "CompletenessAnchor"]
