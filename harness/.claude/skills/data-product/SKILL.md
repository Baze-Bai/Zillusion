---
name: data-product
description: Conventions for the Data Agent — clean user-selected crawled datasets (user-directed, audited) and build data products (reports/charts/datasets/decks) grounded on the real data.
when_to_use: building a data product from one or more crawled datasets; running runtime.cli data; the user asks to clean / analyze / report on harness output
---

# Data product conventions

The **Data Agent** (`runtime/data_agent.py`, CLI `runtime.cli data`) is a full
Claude Code session grounded on real crawled data. It consumes one or more
datasets produced by earlier stages and builds open-ended products. These are
the file/tool conventions it follows (the per-run prompt carries the procedure).

## Layout — `products/<product_id>/`
- `sources/` — staged READ-ONLY copies of the selected datasets. Never modify.
- `clean/` — cleaned datasets you produce.
- `products/` — the deliverables (report.md, charts/, *.xlsx, *.pptx, …).
- `manifest.yaml` — `ProductManifestFile`; `outcome` is gate-computed, do not hand-edit.
- `cleaning_recipe.yaml` — the audit trail of every cleaning step.
- `report.md` — narrative. `feedback.yaml` — data-quality feedback for /explore.

With `--output-root`, `sources/ clean/ products/` move to that drive; the
control files (manifest / recipe / report) stay under `products/<id>/`.

## Data conventions
- **Profile before reasoning.** `profile_dataset(product_id, path)` returns
  per-field coverage / dtypes / top values / numeric range + a small sample.
  Never read a whole dataset into context — compute aggregates with code
  (Python via Bash; `pip install` pandas/matplotlib/openpyxl/python-docx into
  `.venv` when a product needs them).
- Datasets are JSON list-of-records or `{records:[...]}`; CSV/JSONL also load.

## Cleaning is USER-DIRECTED + audited
- Do only the cleaning the user specified (in the task or `--clean-spec`).
  Absent instructions → no destructive cleaning; load as-is and note it.
- `apply_cleaning(product_id, input_path, output_name, steps)` applies an
  ordered step list and writes `clean/` + a recipe (before/after counts). Ops:
  `dedupe(keys?)`, `drop_empty(fields, mode)`, `coerce(field, to)`,
  `filter(field, cmp, value)`, `normalize_ws(fields?)`, `select(fields)`,
  `rename(map)`. `merge_datasets(...)` unions across sources.
- For cleaning the ops can't express, write pandas and log it with
  `record_cleaning_step(...)` so the audit stays complete.

## Products + completion gate
- Write each deliverable under `products/`, then
  `register_product(product_id, path, kind, title)`
  (kind = report|dataset|chart|spreadsheet|deck|document|other).
- Gating dims (completion, NOT quality): `sources_loaded`, `products_produced`,
  `within_budget`. Quality of a product is the reader's call.
- End by reading `read_product_manifest(product_id)` and emitting, as the LAST line:

      [COMPLETE|PARTIAL|FAILED|ABORTED] product_id=<id> sources=<N> products=<M> — <reason>
