"""Pydantic schema for products/<product_id>/manifest.yaml.

Per-run PRODUCT manifest for the **Data Agent** (pipeline's 5th stage — see
``runtime/data_agent.py``). The Data Agent cleans one or more crawled datasets
and builds open-ended data products (reports, charts, derived datasets, decks,
spreadsheets, ...). It records what it produced here AS IT WORKS, and the
overall ``outcome`` is **computed from a gate** over the gating dimensions —
NOT decided ad-hoc by the LLM.

Mirrors ``mcp_server.schemas.run_manifest`` (and reuses its ``Dimension`` via
``scorecard``), but the dimensions judge **completion**, never product
**quality**: a data product (a 研报, a chart) is subjective and has no
independent basis to gate on, so the gate only asks "did it produce a non-empty
deliverable, from non-empty sources, without being killed". Quality is the
human reader's call — same discipline as run_manifest judging completion not
correctness (see memory/reference_verifier_basis_independence).

Gate logic (over GATING dimensions only) — identical shape to run_manifest:

  - ``within_budget`` == fail (killed on a threshold):
        products present -> outcome = partial   (salvaged some deliverable)
        none             -> outcome = aborted
  - else any gating dim == fail              -> outcome = failed
  - else all gating dims in {pass, n/a}      -> outcome = complete
  - else (some gating dim still not_verified)-> outcome = partial

``DataAgentSummary`` parses the agent's final outcome line; it MUST equal this
computed ``outcome`` (upper-cased).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, computed_field

# Reuse the validator's per-dimension record verbatim — same gating/status/
# basis/evidence/notes shape + the same status<->gating legality validator.
from mcp_server.schemas.scorecard import Dimension


ProductOutcome = Literal["complete", "partial", "failed", "aborted"]


class SourceRef(BaseModel):
    """One staged input dataset the product is built from."""

    token: str  # the user-supplied --sources token (site_id / site_id:run_id / path)
    site_id: str | None = None
    run_id: str | None = None
    type: str = "records"  # records | file
    staged_path: str | None = None  # relative path under the product's sources/ dir
    record_count: int | None = None
    field_count: int | None = None
    user_need: str | None = None  # originating "User need:" line from goal.md, if any
    ok: bool = False  # resolved + staged + non-empty
    note: str = ""

    model_config = {"extra": "forbid"}


class ProductEntry(BaseModel):
    """One registered deliverable the Data Agent produced."""

    kind: str = "other"  # report | dataset | chart | spreadsheet | deck | document | other
    path: str  # relative to the product's data dir (or absolute)
    title: str = ""
    bytes: int = 0
    non_empty: bool = False
    notes: str = ""

    model_config = {"extra": "forbid"}


class ProductManifestFile(BaseModel):
    """Top-level shape of products/<product_id>/manifest.yaml."""

    product_id: str
    task: str = ""  # the user's product request (preview)
    started_at: str
    updated_at: str
    output_root: str | None = None  # where bulky data/products live (None = under products/)
    cleaning_applied: bool = False  # True once any cleaning step is recorded
    sources: list[SourceRef] = Field(default_factory=list)
    products: list[ProductEntry] = Field(default_factory=list)
    dimensions: dict[str, Dimension]

    model_config = {"extra": "forbid"}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def outcome(self) -> ProductOutcome:
        """Gate over GATING dimensions. The LLM (+ tool side-effects) fill the
        dimension statuses; this computes the result mechanically."""
        gating = {n: d for n, d in self.dimensions.items() if d.gating}
        if not gating:
            return "aborted"

        out_dim = gating.get("products_produced")
        output_ok = out_dim is not None and out_dim.status == "pass"

        budget = gating.get("within_budget")
        if budget is not None and budget.status == "fail":
            return "partial" if output_ok else "aborted"

        if any(d.status == "fail" for d in gating.values()):
            return "failed"
        if all(d.status in ("pass", "n/a") for d in gating.values()):
            return "complete"
        return "partial"

    @classmethod
    def load(cls, path: Path) -> "ProductManifestFile":
        if not path.exists():
            raise FileNotFoundError(
                f"products/<product_id>/manifest.yaml not found at {path}. "
                f"Initialize with mcp_server.schemas.new_product_manifest() first."
            )
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"products/<product_id>/manifest.yaml at {path} is unparseable YAML: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"products/<product_id>/manifest.yaml at {path} has wrong top-level shape: "
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
            "# products/<product_id>/manifest.yaml — managed by mcp_server.schemas.ProductManifestFile\n"
            "# Per-product run record. `outcome` is COMPUTED (gate over gating dims) — do NOT\n"
            "# hand-edit it. Dimensions judge COMPLETION, never product quality.\n"
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
        """Update one dimension. Validates the status/gating combo via a candidate
        construction (illegal combo raises WITHOUT corrupting the existing entry) —
        same discipline as ScorecardFile/RunManifestFile.set_dimension."""
        if name not in self.dimensions:
            raise KeyError(
                f"dimension {name!r} not in product manifest; known: {sorted(self.dimensions)}"
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

    def add_source(self, src: SourceRef) -> None:
        """Append a staged source (dedupe by token — re-staging replaces)."""
        self.sources = [s for s in self.sources if s.token != src.token]
        self.sources.append(src)

    def add_product(self, prod: ProductEntry) -> None:
        """Append/replace a registered product (dedupe by path)."""
        self.products = [p for p in self.products if p.path != prod.path]
        self.products.append(prod)


# ── Standard dimension catalog (gating?, default basis) ──────────────
# Completion-oriented: did we build a non-empty deliverable, from non-empty
# sources, without being killed. NO quality dimension (quality is subjective).
_PRODUCT_DIMENSIONS: dict[str, tuple[bool, str]] = {
    "sources_loaded": (True, ">=1 selected source resolved + staged non-empty"),
    "products_produced": (True, ">=1 registered product exists + non-empty"),
    "within_budget": (True, "not killed by turn / wall-clock cap"),
    "cleaning_recorded": (
        False,
        "cleaning recipe written when cleaning was requested — advisory",
    ),
}


def new_product_manifest(
    product_id: str, task: str = "", output_root: str | None = None
) -> ProductManifestFile:
    """Fresh product manifest with the completion-dimension catalog, all
    ``not_verified``."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dims = {
        name: Dimension(gating=gating, status="not_verified", basis=basis)
        for name, (gating, basis) in _PRODUCT_DIMENSIONS.items()
    }
    return ProductManifestFile(
        product_id=product_id,
        task=task[:500],
        started_at=now,
        updated_at=now,
        output_root=output_root,
        dimensions=dims,
    )


__all__ = [
    "ProductManifestFile",
    "ProductOutcome",
    "SourceRef",
    "ProductEntry",
    "new_product_manifest",
]
