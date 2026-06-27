"""Pydantic schema for workspaces/<site>/runs/<run_id>/manifest.yaml.

Per-run PRODUCTION manifest. The Run agent records each dimension's verdict
here AS IT WORKS (start → poll → read output → record), and the overall
``outcome`` is **computed from a gate** over the gating dimensions — NOT
decided ad-hoc by the LLM. Mirrors ``mcp_server.schemas.scorecard`` (and
reuses its ``Dimension``), but the dimensions are about **completion** of the
full crawl, not about correctness (quality is the validator's job — a workflow
only reaches the Run agent AFTER it PASSed validation on the sample).

Gate logic (over GATING dimensions only):

  - ``secrets_safe`` == fail (a session-secret leaked into a shipped artifact)
        -> outcome = failed   (overrides everything — never ship a leak)
  - ``within_budget`` == fail (we killed it — threshold OR deliberate agent
    kill_crawl):
        output present  -> outcome = partial   (salvaged partial data)
        no output       -> outcome = aborted    (killed with nothing usable)
  - else any gating dim == fail              -> outcome = failed
  - else all gating dims in {pass, n/a}      -> outcome = complete
  - else (some gating dim still not_verified)-> outcome = partial

This keeps the run outcome auditable: every COMPLETE/PARTIAL/FAILED/ABORTED
traces to specific dimensions with evidence, exactly like the validator's
scorecard. ``RunAgentSummary`` parses the agent's final outcome line and it
MUST equal this computed ``outcome`` (the upper-cased value).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, computed_field

# Reuse the validator's per-dimension record verbatim — same gating/status/
# basis/evidence/notes shape + the same status<->gating legality validator.
from mcp_server.schemas.scorecard import Dimension


RunOutcome = Literal["complete", "partial", "failed", "aborted"]


class RunManifestFile(BaseModel):
    """Top-level shape of workspaces/<site>/runs/<run_id>/manifest.yaml."""

    run_id: str
    site_id: str
    workflow_type: str = "extraction"  # extraction | download | api
    crawl_mode: str = "full"  # full | sample (what CRAWL_MODE was set to)
    started_at: str
    updated_at: str
    record_count: int | None = None  # records in output.json (extraction) or files (download)
    output_path: str | None = None  # relative path to the kept deliverable
    # field/unit name -> meaning. The data-definition declaration for runs whose
    # records are weak-schema DATA UNITS (documents / media / corpus pages)
    # rather than table rows — written by init_crawl / commit_inline_dataset.
    data_definition: dict[str, str] | None = None
    dimensions: dict[str, Dimension]

    model_config = {"extra": "forbid"}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def outcome(self) -> RunOutcome:
        """Gate over GATING dimensions. The LLM fills dimension statuses; this
        computes the result mechanically (see module docstring for the gate)."""
        gating = {n: d for n, d in self.dimensions.items() if d.gating}
        if not gating:
            return "aborted"  # nothing ran

        # A shipped-secret leak is the MOST severe failure — it overrides
        # completion (a leaking dataset must never read as complete OR as a
        # shippable partial). Only present in the agentic catalog.
        secrets = gating.get("secrets_safe")
        if secrets is not None and secrets.status == "fail":
            return "failed"

        # "Did we keep usable output?" — produced_output (extraction) or
        # files_present (download), whichever this catalog has.
        out_dim = gating.get("produced_output") or gating.get("files_present")
        output_ok = out_dim is not None and out_dim.status == "pass"

        budget = gating.get("within_budget")
        if budget is not None and budget.status == "fail":
            # We killed it (threshold or deliberate agent kill) — the
            # proximate reason dominates.
            return "partial" if output_ok else "aborted"

        # Agentic-crawl only: the agent finished its loop but didn't reach the
        # completeness anchor (e.g. hard anchor 25104, only 8000 scraped before
        # an unclearable wall). Not budget-killed — it stopped itself — but
        # incomplete: kept data => partial, nothing => aborted. Mirrors the
        # budget branch so an incomplete harvest never reads as a clean FAILED.
        completeness = gating.get("completeness")
        if completeness is not None and completeness.status == "fail":
            return "partial" if output_ok else "aborted"

        if any(d.status == "fail" for d in gating.values()):
            return "failed"
        if all(d.status in ("pass", "n/a") for d in gating.values()):
            return "complete"
        return "partial"  # some gating dim still not_verified

    @classmethod
    def load(cls, path: Path) -> "RunManifestFile":
        if not path.exists():
            raise FileNotFoundError(
                f"runs/<run_id>/manifest.yaml not found at {path}. "
                f"Initialize with mcp_server.schemas.new_run_manifest() first."
            )
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"runs/<run_id>/manifest.yaml at {path} is unparseable YAML: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"runs/<run_id>/manifest.yaml at {path} has wrong top-level shape: "
                f"got {type(data).__name__}, expected mapping."
            )
        # `outcome` is COMPUTED; drop any persisted copy so extra=forbid doesn't
        # reject it on reload, and so it's always recomputed fresh.
        data.pop("outcome", None)
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        """Serialize (incl. computed ``outcome`` for human read) + atomic write."""
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = self.model_dump()  # includes computed outcome
        text = yaml.safe_dump(
            serialized,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=100,
        )
        header = (
            "# workspaces/<site>/runs/<run_id>/manifest.yaml — managed by mcp_server.schemas.RunManifestFile\n"
            "# Per-dimension PRODUCTION run record. `outcome` is COMPUTED (gate over gating dims) —\n"
            "# do NOT hand-edit it. Update each dimension's status / basis / evidence as the crawl runs.\n"
            "\n"
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(header + text, encoding="utf-8")
        tmp.replace(path)

    def set_dimension(
        self,
        name: str,
        status: str,
        *,
        basis: str | None = None,
        evidence: str | None = None,
        notes: str | None = None,
    ) -> Dimension:
        """Update one dimension. Validates the status/gating combo via a
        candidate construction (so an illegal combo raises WITHOUT corrupting
        the existing entry) — same discipline as ScorecardFile.set_dimension."""
        if name not in self.dimensions:
            raise KeyError(
                f"dimension {name!r} not in run manifest; known: {sorted(self.dimensions)}"
            )
        cur = self.dimensions[name]
        candidate = Dimension(
            gating=cur.gating,
            status=status,  # type: ignore[arg-type]  # validated by Dimension's model_validator
            basis=basis if basis is not None else cur.basis,
            evidence=evidence if evidence is not None else cur.evidence,
            notes=notes if notes is not None else cur.notes,
        )
        self.dimensions[name] = candidate
        return candidate


# ── Standard dimension catalogs (gating?, default basis) ─────────────
# Completion-oriented: did the FULL crawl run to the end and keep usable data.
_EXTRACTION_DIMENSIONS: dict[str, tuple[bool, str]] = {
    "launched": (True, "subprocess spawned"),
    "ran_clean": (True, "runtime (exit_code 0, no traceback)"),
    "produced_output": (True, "output.json exists, parses, non-empty record list"),
    "non_trivial": (True, "record_count > 0 (evidence notes full-vs-sample delta)"),
    "within_budget": (True, "not killed (wall-clock / stall threshold or deliberate agent kill)"),
    "partial_failures": (False, "per-record failure rate in the output — advisory"),
    "full_mode_effective": (
        False,
        "did CRAWL_MODE=full take effect — advisory (legacy ignores it)",
    ),
}

# Download workflows: usable output = the declared file(s) present + non-empty.
_DOWNLOAD_DIMENSIONS: dict[str, tuple[bool, str]] = {
    "launched": (True, "subprocess spawned"),
    "ran_clean": (True, "runtime (exit_code 0, no traceback)"),
    "files_present": (True, "declared download_manifest files exist + non-empty in output dir"),
    "within_budget": (True, "not killed (wall-clock / stall threshold or deliberate agent kill)"),
    "partial_failures": (False, "per-file download failure rate — advisory"),
    "full_mode_effective": (
        False,
        "did CRAWL_MODE=full take effect — advisory (legacy ignores it)",
    ),
}

# Agentic-crawl route: NO deterministic subprocess — a persistent SDK agent
# drives the browser, writes code, commits records to records.jsonl until it
# judges (against the crawl_brief's completeness anchor) that it's done. So the
# dimensions differ from the deterministic catalogs: there's no exit_code to
# check (drop ran_clean) and no CRAWL_MODE sample/full toggle (drop
# full_mode_effective); instead `completeness` (gating) records the agent's
# anchor-grounded done judgment, and within_budget covers a progress-stall /
# wall-clock kill of the agent loop itself.
_AGENTIC_DIMENSIONS: dict[str, tuple[bool, str]] = {
    "launched": (True, "agent session started + bound to the workspace"),
    "produced_output": (True, "records.jsonl exists, parses, non-empty record list"),
    "self_consistency": (
        True,
        "internal cross-artifact agreement (reproduction-free): every committed "
        "record carries source_url + a content carrier; declared crawl_brief "
        "field_schema keys vs produced record keys; file_ref targets exist + non-empty",
    ),
    "completeness": (
        True,
        "reached the completeness anchor (hard: cursor to full set, every unit "
        "scraped or tomb-stoned; soft: stop heuristic met + justified)",
    ),
    "within_budget": (True, "not killed (wall-clock / progress-stall threshold)"),
    "secrets_safe": (
        True,
        "no auth_state.json session-secret (cookie / localStorage token) literal in "
        "the shipped run artifacts; n/a when the run used no takeover auth",
    ),
    "partial_failures": (False, "skipped/failed-unit rate (tombstones) — advisory"),
}

_DIMENSIONS_BY_TYPE: dict[str, dict[str, tuple[bool, str]]] = {
    "extraction": _EXTRACTION_DIMENSIONS,
    "download": _DOWNLOAD_DIMENSIONS,
    # API workflows produce records exactly like extraction (output.json,
    # record_count) — same completion dimensions apply.
    "api": _EXTRACTION_DIMENSIONS,
    # Agentic-crawl route (route, not output type — produces records like
    # extraction but via a persistent agent loop, not a workflow.py).
    "agentic": _AGENTIC_DIMENSIONS,
}


def new_run_manifest(
    run_id: str,
    site_id: str,
    workflow_type: str = "extraction",
    crawl_mode: str = "full",
    data_definition: dict[str, str] | None = None,
) -> RunManifestFile:
    """Fresh manifest with the completion-dimension catalog for ``workflow_type``
    (``extraction`` default; ``download`` = file-completion dims), all
    ``not_verified``. Unknown types fall back to the extraction catalog."""
    catalog = _DIMENSIONS_BY_TYPE.get(workflow_type, _EXTRACTION_DIMENSIONS)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dims = {
        name: Dimension(gating=gating, status="not_verified", basis=basis)
        for name, (gating, basis) in catalog.items()
    }
    return RunManifestFile(
        run_id=run_id,
        site_id=site_id,
        workflow_type=workflow_type,
        crawl_mode=crawl_mode,
        started_at=now,
        updated_at=now,
        data_definition=data_definition,
        dimensions=dims,
    )


__all__ = [
    "RunManifestFile",
    "RunOutcome",
    "new_run_manifest",
]
