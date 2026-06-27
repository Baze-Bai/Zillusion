"""Pydantic schema for workspaces/<site>/hypotheses.yaml.

Canonical shape: top-level YAML list of Hypothesis objects.

Why a list (not a mapping)?
  - Append-friendly: hypothesis-loop adds new entries over a run (some
    folded in from the validator's feedback.yaml); with a list, "append"
    is well-defined; with a mapping it requires synthesizing unique keys.
  - Ordered: the chronological order of hypothesis discovery carries
    information (early findings vs late refinements).

Migration note: hypothesis-loop previously wrote a YAML mapping. As of
2026-05-22 it should write through `Workspace.write_hypotheses()` which
serializes a `HypothesesFile` — i.e. a list. Pre-existing mapping-style
files will fail loudly at load time; resolution is to re-run /explore
or convert manually (e.g. wrap each block as a list item with `id`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, RootModel, field_validator, model_validator


HypothesisStatus = Literal["unverified", "confirmed", "refuted", "partial", "blocked"]
HypothesisPriority = Literal["low", "medium", "high"]
WallType = Literal["login", "login_modal", "captcha", "challenge", "none"]


class Hypothesis(BaseModel):
    """One hypothesis in the exploration / validation loop."""

    id: str = Field(..., description="Stable identifier, e.g. 'H001'. Must be unique within the file.")
    claim: str = Field(..., min_length=1, description="One-sentence claim being tested.")
    source: str = Field(..., description="Where this hypothesis came from (e.g. '/explore <date>' or 'validation-agent run <val-XXXX>').")
    status: HypothesisStatus = "unverified"
    priority: HypothesisPriority = "medium"
    result: str | None = Field(default=None, description="Outcome description; null until status flips to confirmed/refuted.")
    notes: str | None = Field(default=None, description="Free-form context, links to validation_report.md sections, evidence pointers.")
    wall_type: WallType | None = Field(default=None, description="Access-wall type, mirroring browser_check_login_wall's wall_type; recorded when a wall is why this hypothesis is blocked. null when not wall-related.")

    model_config = {"extra": "forbid"}

    @field_validator("id")
    @classmethod
    def _id_must_be_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Hypothesis.id must be non-empty")
        return v

    @model_validator(mode="after")
    def _wall_type_requires_blocked(self) -> "Hypothesis":
        if self.wall_type and self.wall_type != "none" and self.status != "blocked":
            raise ValueError(
                f"wall_type={self.wall_type!r} requires status='blocked'; got status={self.status!r}."
            )
        return self


class HypothesesFile(RootModel[list[Hypothesis]]):
    """Top-level shape of workspaces/<site>/hypotheses.yaml.

    A YAML list of Hypothesis objects, deserialized as a Python list and
    serialized back the same way. Use the IO helpers below — do not
    construct paths and call yaml.safe_load directly elsewhere.
    """

    @classmethod
    def load(cls, path: Path) -> "HypothesesFile":
        """Read + parse + validate. Raises with a clear message on every
        failure mode (missing, unparseable, wrong shape, schema mismatch)."""
        if not path.exists():
            raise FileNotFoundError(
                f"hypotheses.yaml not found at {path}. "
                f"Run /explore <site> first to initialize."
            )
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"hypotheses.yaml at {path} is unparseable YAML: {exc}. "
                f"Do NOT append further; fix the file structure first "
                f"(or re-run /explore to regenerate)."
            ) from exc
        if data is None:
            return cls.model_validate([])
        if not isinstance(data, list):
            raise ValueError(
                f"hypotheses.yaml at {path} has wrong top-level shape: "
                f"got {type(data).__name__}, expected list. "
                f"The canonical schema is a YAML list of hypothesis objects "
                f"(see mcp_server/schemas/hypotheses.py). Resolution: re-run "
                f"/explore (which writes through write_hypotheses), or convert "
                f"the file manually."
            )
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        """Serialize to YAML and write atomically.

        Always writes a list — even if empty. Never produces a mapping.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = [h.model_dump(exclude_none=False) for h in self.root]
        text = yaml.safe_dump(
            serialized,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        # Add a leading comment so the file is self-describing if someone opens it
        header = (
            "# workspaces/<site>/hypotheses.yaml — managed by mcp_server.schemas.HypothesesFile\n"
            "# Top-level shape: YAML list of Hypothesis objects.\n"
            "# Do NOT edit the structure by hand; use hypothesis_append / hypothesis_set_status\n"
            "# MCP tools, or write through Workspace.write_hypotheses(). Any append done as a\n"
            "# raw string concat will likely corrupt the YAML — guard your tools accordingly.\n"
            "\n"
        )
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(header + text, encoding="utf-8")
        tmp_path.replace(path)

    def append(self, hypothesis: Hypothesis) -> None:
        """Add a new hypothesis. Raises if id collides with existing."""
        existing_ids = {h.id for h in self.root}
        if hypothesis.id in existing_ids:
            raise ValueError(
                f"Hypothesis.id={hypothesis.id!r} already exists in this file. "
                f"Use a unique id or call set_status() to update the existing one."
            )
        self.root.append(hypothesis)

    def get(self, hypothesis_id: str) -> Hypothesis | None:
        for h in self.root:
            if h.id == hypothesis_id:
                return h
        return None

    def set_status(
        self,
        hypothesis_id: str,
        status: HypothesisStatus,
        result: str | None = None,
        notes: str | None = None,
        wall_type: "WallType | None" = None,
    ) -> Hypothesis:
        """Update status (and optionally result/notes/wall_type) of an existing hypothesis."""
        target = self.get(hypothesis_id)
        if target is None:
            raise KeyError(
                f"Hypothesis id={hypothesis_id!r} not found. "
                f"Existing ids: {sorted(h.id for h in self.root)}"
            )
        target.status = status
        if result is not None:
            target.result = result
        if notes is not None:
            target.notes = notes
        if wall_type is not None:
            target.wall_type = wall_type
        # Direct attribute mutation bypasses model validators; re-validate the
        # final state so the wall_type ⇒ blocked invariant still holds.
        Hypothesis.model_validate(target.model_dump())
        return target

    def next_id(self, prefix: str = "H", width: int = 3) -> str:
        """Compute the next sequential id (e.g. 'H010' after 'H001'..'H009').

        Scans existing ids for ones matching `<prefix><digits>` and returns
        the next integer in that sequence. Falls back to `<prefix>001` if no
        matching ids exist. Width is the zero-pad length of the digit suffix.
        """
        max_n = 0
        for h in self.root:
            if h.id.startswith(prefix):
                tail = h.id[len(prefix):]
                if tail.isdigit():
                    max_n = max(max_n, int(tail))
        return f"{prefix}{max_n + 1:0{width}d}"


# ──────────────────────────────────────────────────────────────────────
# Convenience top-level helpers (used by validation-agent + tests)
# ──────────────────────────────────────────────────────────────────────


def append_hypothesis_to_file(
    path: Path,
    *,
    claim: str,
    source: str,
    status: HypothesisStatus = "unverified",
    priority: HypothesisPriority = "high",
    notes: str | None = None,
    result: str | None = None,
    id_prefix: str = "H",
) -> Hypothesis:
    """One-shot: load, append (with auto-id), save. Raises clear errors
    on every schema mismatch. This is the **only** safe path for
    append-from-outside; validation-agent uses this via MCP tool."""
    file = HypothesesFile.load(path) if path.exists() else HypothesesFile.model_validate([])
    new_id = file.next_id(prefix=id_prefix)
    new_h = Hypothesis(
        id=new_id,
        claim=claim,
        source=source,
        status=status,
        priority=priority,
        result=result,
        notes=notes,
    )
    file.append(new_h)
    file.save(path)
    return new_h


def utcnow_iso() -> str:
    """Helper for `source: validation-agent run <id> on <ISO>` strings."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "Hypothesis",
    "HypothesesFile",
    "HypothesisStatus",
    "HypothesisPriority",
    "WallType",
    "append_hypothesis_to_file",
    "utcnow_iso",
]
