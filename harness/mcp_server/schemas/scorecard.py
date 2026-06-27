"""Pydantic schema for workspaces/<site>/validation/<run_id>/scorecard.yaml.

Per-run validation scorecard. The validator records each dimension's
verdict here AS IT WORKS (read + update over the run), and the overall
verdict is **computed from a gate** over the gating dimensions — NOT
decided ad-hoc by the LLM. This makes the verdict auditable: every
PASS/FAIL traces to specific dimensions with evidence.

Gate logic (over GATING dimensions only):

  - any gating dim == fail            -> overall = fail
  - all gating dims in {pass, n/a}    -> overall = pass   (success)
  - otherwise (some still not_verified)-> overall = inconclusive

This encodes the hard-won discipline (see
memory/reference_verifier_basis_independence.md):

  - Only dimensions with an INDEPENDENT BASIS gate success
    (syntax/runs/self-consistency/reproducibility/resample/field-semantics).
  - Soft-review dims (field_reasonableness) and non-verifiable dims
    (completeness) are recorded + feed hypotheses, but DON'T gate —
    they have no basis independent of the explore agent.
  - not_verified (sparse field's condition never triggered, data
    missing) -> inconclusive, NOT pass: "tolerated (not a FAIL) but
    NOT verified-OK either."
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, computed_field, model_validator


DimensionStatus = Literal["pass", "fail", "not_verified", "advisory", "n/a"]
OverallVerdict = Literal["pass", "fail", "inconclusive"]


class Dimension(BaseModel):
    """One validation dimension's record."""

    gating: bool = Field(
        ...,
        description=(
            "Whether this dimension gates success. Hard-verifiable dims (an "
            "independent basis exists) gate; soft-review / non-verifiable dims don't."
        ),
    )
    status: DimensionStatus = "not_verified"
    basis: str = Field(
        default="",
        description="Independent basis used to judge — 'code' / 'live page' / 'internal consistency' / 'vague goal' / 'none (loop)'.",
    )
    evidence: str = Field(default="", description="Concrete evidence for the status: tool output, page observation, diff.")
    notes: str = Field(default="", description="Free notes — why not_verified, the advisory suggestion, etc.")

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_status_gating_combo(self) -> "Dimension":
        # advisory is a soft-review status — only valid on non-gating dims.
        if self.gating and self.status == "advisory":
            raise ValueError(
                "gating dimension cannot have status 'advisory' "
                "(advisory is for non-gating soft review)"
            )
        # pass/fail are gate verdicts — meaningless on a dim that doesn't gate.
        if not self.gating and self.status in ("pass", "fail"):
            raise ValueError(
                f"non-gating dimension cannot have status {self.status!r} "
                f"(it doesn't gate; use advisory / not_verified / n/a)"
            )
        return self


class ScorecardFile(BaseModel):
    """Top-level shape of workspaces/<site>/validation/<run_id>/scorecard.yaml."""

    run_id: str
    site_id: str
    started_at: str
    updated_at: str
    dimensions: dict[str, Dimension]

    model_config = {"extra": "forbid"}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overall(self) -> OverallVerdict:
        """Gate over GATING dimensions only. This is THE verdict — the LLM
        fills dimension statuses, this computes the result mechanically."""
        gating = [d for d in self.dimensions.values() if d.gating]
        if not gating:
            return "inconclusive"  # nothing hard was verified
        if any(d.status == "fail" for d in gating):
            return "fail"
        if all(d.status in ("pass", "n/a") for d in gating):
            return "pass"
        return "inconclusive"  # some gating dim still not_verified

    @classmethod
    def load(cls, path: Path) -> "ScorecardFile":
        if not path.exists():
            raise FileNotFoundError(
                f"validation/<run_id>/scorecard.yaml not found at {path}. "
                f"Initialize with mcp_server.schemas.new_scorecard() first."
            )
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"validation/<run_id>/scorecard.yaml at {path} is unparseable YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"validation/<run_id>/scorecard.yaml at {path} has wrong top-level shape: "
                f"got {type(data).__name__}, expected mapping."
            )
        # `overall` is COMPUTED; drop any persisted copy so extra=forbid
        # doesn't reject it on reload, and so it's always recomputed fresh.
        data.pop("overall", None)
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        """Serialize (incl. computed `overall` for human read) + atomic write."""
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = self.model_dump()  # includes computed overall
        text = yaml.safe_dump(
            serialized, allow_unicode=True, sort_keys=False,
            default_flow_style=False, width=100,
        )
        header = (
            "# workspaces/<site>/validation/<run_id>/scorecard.yaml — managed by mcp_server.schemas.ScorecardFile\n"
            "# Per-dimension validation record. `overall` is COMPUTED (gate over gating dims) —\n"
            "# do NOT hand-edit it. Update each dimension's status / basis / evidence as you verify.\n"
            "\n"
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(header + text, encoding="utf-8")
        tmp.replace(path)

    def set_dimension(
        self,
        name: str,
        status: DimensionStatus,
        *,
        basis: str | None = None,
        evidence: str | None = None,
        notes: str | None = None,
    ) -> Dimension:
        """Update one dimension. Validates the status/gating combo via a
        candidate construction (so an illegal combo raises WITHOUT corrupting
        the existing entry)."""
        if name not in self.dimensions:
            raise KeyError(
                f"dimension {name!r} not in scorecard; known: {sorted(self.dimensions)}"
            )
        cur = self.dimensions[name]
        candidate = Dimension(
            gating=cur.gating,
            status=status,
            basis=basis if basis is not None else cur.basis,
            evidence=evidence if evidence is not None else cur.evidence,
            notes=notes if notes is not None else cur.notes,
        )  # model_validator fires here; illegal combo raises, cur untouched
        self.dimensions[name] = candidate
        return candidate


# ── Standard dimension catalog (gating?, default basis) ──────────────
# 6 gating (independent basis) + 2 non-gating (soft / loop-only).
_DEFAULT_DIMENSIONS: dict[str, tuple[bool, str]] = {
    "syntax":               (True,  "code (ast.parse)"),
    "runs_clean":           (True,  "runtime (exit_code, no traceback)"),
    "self_consistency":     (True,  "internal (declared == produced; 3-way selectors.yaml/workflow.py/output)"),
    "reproducibility":      (True,  "cold re-run vs output_sample.json"),
    "resample_match":       (True,  "re-fetch with extract_js vs output_sample.json"),
    "field_semantics":      (True,  "live page (inspect_record_html) — selector grabs the RIGHT field"),
    "field_reasonableness": (False, "vague goal.md intent — soft review, advisory only"),
    "completeness":         (False, "no single-run basis — multi-round explore-validate loop outcome"),
}

# Download-workflow dimension catalog (file data sources). 6 gating + 1 non-gating.
# The gate logic (overall) is identical; only the dimensions differ — a download
# workflow has no selectors/records, so it's verified against the downloaded file.
_DOWNLOAD_DIMENSIONS: dict[str, tuple[bool, str]] = {
    "syntax":          (True,  "code (ast.parse)"),
    "runs_clean":      (True,  "runtime (exit_code, no traceback)"),
    "file_downloaded": (True,  "file exists on disk after an isolated re-run"),
    "non_empty":       (True,  "file size >= declared min_bytes"),
    "format_valid":    (True,  "bytes match the declared format (magic bytes / extension)"),
    "parses":          (True,  "the format parser loads it (csv rows / json / xlsx / ...)"),
    "completeness":    (False, "got ALL files the source offers — loop-only, no single-run basis"),
}

# API-workflow dimension catalog (HTTP API data sources). 6 gating + 2 non-gating.
# Records out, like extraction — but ground truth is the LIVE API response
# (probe_api_endpoint re-calls the manifest's probe_url), not a rendered page,
# and a secret-leak scan gates because workflow.py handles user credentials.
# No separate field_semantics dim: wrong field wiring on an API surfaces as
# output values absent from the live response, which endpoint_match covers.
_API_DIMENSIONS: dict[str, tuple[bool, str]] = {
    "syntax":               (True,  "code (ast.parse)"),
    "runs_clean":           (True,  "runtime (exit_code, no traceback)"),
    "self_consistency":     (True,  "internal (api_manifest.yaml declared fields == produced output keys)"),
    "reproducibility":      (True,  "cold isolated re-run vs output_sample.json"),
    "endpoint_match":       (True,  "live API re-call (probe_api_endpoint) — output values present in the real response"),
    "secrets_safe":         (True,  "no credential literal in shipped artifacts; n/a when no credentials"),
    "field_reasonableness": (False, "vague goal.md intent — soft review, advisory only"),
    "completeness":         (False, "no single-run basis — multi-round explore-validate loop outcome"),
}

_DIMENSIONS_BY_TYPE: dict[str, dict[str, tuple[bool, str]]] = {
    "extraction": _DEFAULT_DIMENSIONS,
    "download": _DOWNLOAD_DIMENSIONS,
    "api": _API_DIMENSIONS,
}


def new_scorecard(run_id: str, site_id: str, workflow_type: str = "extraction") -> ScorecardFile:
    """Fresh scorecard with the dimension catalog for ``workflow_type``
    (``extraction`` = the 8-dim default; ``download`` = file-download dims),
    all ``not_verified``. Unknown types fall back to the extraction catalog."""
    catalog = _DIMENSIONS_BY_TYPE.get(workflow_type, _DEFAULT_DIMENSIONS)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dims = {
        name: Dimension(gating=gating, status="not_verified", basis=basis)
        for name, (gating, basis) in catalog.items()
    }
    return ScorecardFile(run_id=run_id, site_id=site_id, started_at=now, updated_at=now, dimensions=dims)


__all__ = [
    "Dimension",
    "ScorecardFile",
    "DimensionStatus",
    "OverallVerdict",
    "new_scorecard",
]
