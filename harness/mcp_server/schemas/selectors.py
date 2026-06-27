"""Pydantic schema for workspaces/<site>/selectors.yaml.

Canonical shape: top-level YAML mapping with two blocks, both required:

  selectors:           # field selectors + extract_js for verify
    observed_at: ISO
    source_url: url
    records_observed: int
    record_locator: css
    fields:
      <field_name>:
        selector: css | null
        extraction: str
        stability: stable | positional | fragile
        observation: str
        fallback_selector: css | null
    extract_js: |
      () => { /* JS, returns list[dict] */ }

  field_stability:     # cross-call drift classification for verify
    observed_at: ISO
    source_url: url
    probe_count: int
    fields:
      <field_name>:
        drifted: bool
        observation: str
        suggested_class: STRICT | TOLERANT | SKIP
        drift_examples: list[str] | null

This file is **/explore-managed**: hypothesis-loop writes it, then
the validator reads it. The validator is read-only on every explore
artifact (it writes only `validation/<run_id>/`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


SelectorStability = Literal["stable", "positional", "fragile"]
FieldStabilityClass = Literal["STRICT", "TOLERANT", "SKIP"]


class SelectorEntry(BaseModel):
    """One field's listing-page extraction descriptor.

    The dict KEY this entry is stored under (in SelectorsBlock.fields) is the
    *canonical* field name — what the output schema / goal.md calls it. The
    two fields below let the downstream validator cross-check that the
    workflow actually wired this selector to the right place:

      - `workflow_field` lets validation catch NAMING drift (the selector is
        declared for `vote_count` but workflow.py writes it into `votes`).
      - `semantic` lets validation catch SEMANTIC mis-wiring (the selector
        declared for `vote_count` actually scrapes the comment count).
    """

    selector: str | None = Field(
        None, description="CSS selector, or null for workflow-internal fields."
    )
    extraction: str = Field(..., description="One-line description of how the value is computed.")
    stability: SelectorStability = "positional"
    observation: str = Field(
        default="", description="What was observed; surfaces in validation reports."
    )
    fallback_selector: str | None = None
    workflow_field: str | None = Field(
        default=None,
        description=(
            "The variable / output-dict key name workflow.py uses for this field. "
            "None means it matches this entry's dict key (the common case). Set it "
            "explicitly only when workflow.py uses a DIFFERENT name, so the validator "
            "can flag naming drift between the selector catalog and the workflow."
        ),
    )
    semantic: str = Field(
        default="",
        description=(
            "What real-world concept this field captures, stated unambiguously "
            "(e.g. 'upvote score, NOT the comment count'). Lets the validator check "
            "the selector maps to the RIGHT field — catches mis-wiring like a "
            "comment-count selector assigned to vote_count."
        ),
    )

    model_config = {"extra": "forbid"}


class SelectorsBlock(BaseModel):
    """The `selectors:` block — selector catalog + extract_js."""

    observed_at: str = Field(..., description="ISO timestamp when the agent recorded these.")
    source_url: str = Field(..., description="Listing URL the selectors were observed against.")
    records_observed: int = Field(..., ge=0)
    record_locator: str = Field(..., description="CSS that matches each listing card wrapper.")
    fields: dict[str, SelectorEntry] = Field(
        ..., description="Per-field selector entries; keys are output schema field names."
    )
    extract_js: str = Field(
        ...,
        min_length=1,
        description="Nullary JS function (as text) that returns list[dict]; used by the validator's resample_and_compare via page.evaluate().",
    )

    model_config = {"extra": "forbid"}


class FieldStabilityEntry(BaseModel):
    """One field's drift classification (drives the validator's compare_output STRICT/TOLERANT/SKIP decision)."""

    drifted: bool = Field(..., description="Has this field been observed to drift across re-runs?")
    observation: str = Field(
        default="", description="What was seen; surfaces in validation reports."
    )
    suggested_class: FieldStabilityClass = Field(
        ...,
        description="STRICT (exact match), TOLERANT (numeric ±5/5%; long text: small length drift with an identical opening), or SKIP (not compared).",
    )
    drift_examples: list[str] | None = None

    model_config = {"extra": "forbid"}


class FieldStabilityBlock(BaseModel):
    """The `field_stability:` block — empirical drift classification."""

    observed_at: str
    source_url: str
    probe_count: int = Field(..., ge=0)
    fields: dict[str, FieldStabilityEntry]

    model_config = {"extra": "forbid"}


class SelectorsFile(BaseModel):
    """Top-level shape of workspaces/<site>/selectors.yaml.

    Both `selectors` and `field_stability` are required; the validator
    needs both. If a workspace genuinely has nothing to classify, both blocks
    must still be present with empty `fields: {}`.
    """

    selectors: SelectorsBlock
    field_stability: FieldStabilityBlock

    model_config = {"extra": "forbid"}

    @classmethod
    def load(cls, path: Path) -> "SelectorsFile":
        if not path.exists():
            raise FileNotFoundError(
                f"selectors.yaml not found at {path}. "
                f"The /explore agent (hypothesis-loop skill) must write this "
                f"during exploration before validation-agent runs."
            )
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"selectors.yaml at {path} is unparseable YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"selectors.yaml at {path} has wrong top-level shape: "
                f"got {type(data).__name__}, expected mapping with "
                f"'selectors' and 'field_stability' keys."
            )
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Note: extract_js needs literal block style to stay readable.
        # PyYAML's default_flow_style=False yields '|' block style for
        # multi-line strings, which is exactly what we want.
        serialized = self.model_dump()
        text = yaml.safe_dump(
            serialized,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=120,
        )
        header = (
            "# workspaces/<site>/selectors.yaml — managed by mcp_server.schemas.SelectorsFile\n"
            "# Top-level keys: `selectors` (extract catalog) + `field_stability` (drift classes).\n"
            "# Written by /explore (hypothesis-loop), read by the validator.\n"
            "\n"
        )
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(header + text, encoding="utf-8")
        tmp_path.replace(path)


__all__ = [
    "SelectorEntry",
    "SelectorsBlock",
    "FieldStabilityEntry",
    "FieldStabilityBlock",
    "SelectorsFile",
    "SelectorStability",
    "FieldStabilityClass",
]
