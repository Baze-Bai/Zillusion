"""In-process SDK MCP tool surface for the Data Agent (``mcp__data__*``).

Unlike the validator/run tools (which ARE the agent's entire capability surface),
this server is **additive**: the Data Agent keeps the full Claude Code toolset
(Read/Write/Edit/Bash/Glob/Grep/WebFetch + skills) and these tools just make the
crawled data convenient + keep the product run auditable. The agent can always
fall back to writing pandas via Bash; these exist so the common path (profile →
clean-with-audit → register) is one reliable call each.

Writes target ``products/<product_id>/`` only (control dir on E:; bulky
clean/products under --output-root if set). The deterministic kill/sizing knobs
(sample_rows / top_n / max_field_chars / output_root) are injected via
``build_data_server(config)`` — the agent does not own them.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from runtime.validator_checks import PROJECT_ROOT
from runtime.validator_tools import _ok, _safe, _schema
from runtime.data_prep import (
    _disp,
    _under,
    apply_cleaning_steps,
    load_any,
    profile_path,
    resolve_dirs,
    resolve_source,
)
from mcp_server.schemas import ProductEntry, ProductManifestFile


def _rp(data_dir: Path, path: str) -> Path:
    """Resolve a tool path arg: absolute as-is, else relative to the product
    data dir (so 'sources/x.json' / 'products/report.md' work)."""
    p = Path(path)
    return p if p.is_absolute() else (Path(data_dir) / path)


# ── recipe + manifest helpers (control dir only) ─────────────────────


def _append_recipe(control: Path, entry: dict) -> None:
    control.mkdir(parents=True, exist_ok=True)
    fp = control / "cleaning_recipe.yaml"
    data = yaml.safe_load(fp.read_text(encoding="utf-8")) if fp.exists() else None
    items = data if isinstance(data, list) else []
    items.append(entry)
    fp.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _mark_cleaning(control: Path) -> None:
    """Flag the manifest that cleaning happened (cleaning_applied + advisory dim)."""
    mp = control / "manifest.yaml"
    if not mp.exists():
        return
    try:
        m = ProductManifestFile.load(mp)
        m.cleaning_applied = True
        m.set_dimension("cleaning_recorded", "advisory", basis="cleaning recipe written")
        m.save(mp)
    except Exception:  # noqa: BLE001 — never let bookkeeping abort the tool
        pass


# ── tool backends ────────────────────────────────────────────────────


def list_available_sources() -> dict:
    """Workspaces that have a crawled dataset ready to consume (latest run /
    output_sample). Cheap — does not load record bodies."""
    ws_root = PROJECT_ROOT / "workspaces"
    out = []
    if ws_root.is_dir():
        for site in sorted(ws_root.iterdir()):
            if not site.is_dir():
                continue
            d = resolve_source(site.name)
            if not d["exists"]:
                continue
            out.append(
                {
                    "site_id": site.name,
                    "run_id": d.get("run_id"),
                    "type": d["type"],
                    "user_need": d.get("user_need"),
                    "source_path": d["source_path"],
                }
            )
    return {"count": len(out), "sources": out}


def profile_dataset(product_id, path, output_root, sample_rows, top_n, max_field_chars) -> dict:
    _, data = resolve_dirs(product_id, output_root)
    return profile_path(
        _rp(data, path), sample_rows=sample_rows, top_n=top_n, max_field_chars=max_field_chars
    )


def read_records(product_id, path, output_root, offset, limit, max_field_chars) -> dict:
    _, data = resolve_dirs(product_id, output_root)
    recs = load_any(_rp(data, path))
    if recs is None:
        return {
            "error": "not a loadable record list (json/jsonl/csv)",
            "path": str(_rp(data, path)),
        }
    window = recs[offset : offset + limit]
    rows = [{k: _disp(v, max_field_chars) for k, v in r.items()} for r in window]
    return {
        "total": len(recs),
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "records": rows,
    }


def apply_cleaning(product_id, input_path, output_name, steps, output_root) -> dict:
    control, data = resolve_dirs(product_id, output_root)
    src = _rp(data, input_path)
    records = load_any(src)
    if records is None:
        return {
            "error": "input not a loadable record list (json/jsonl/csv)",
            "input_path": str(src),
        }
    before = len(records)
    cleaned, stats = apply_cleaning_steps(records, steps)
    clean_dir = data / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    name = output_name if output_name.lower().endswith(".json") else output_name + ".json"
    out = clean_dir / name
    out.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_recipe(
        control,
        {
            "input": str(src),
            "output": str(out),
            "total_before": before,
            "total_after": len(cleaned),
            "steps": stats,
        },
    )
    _mark_cleaning(control)
    return {
        "output_path": str(out),
        "rel_output": str(out.relative_to(data)) if _under(out, data) else str(out),
        "before": before,
        "after": len(cleaned),
        "steps": stats,
    }


def merge_datasets(product_id, inputs, output_name, source_field, output_root) -> dict:
    control, data = resolve_dirs(product_id, output_root)
    merged: list[dict] = []
    per = []
    for inp in inputs:
        recs = load_any(_rp(data, inp))
        if recs is None:
            per.append({"input": inp, "loaded": 0, "error": "unloadable"})
            continue
        if source_field:
            for r in recs:
                r.setdefault(source_field, Path(inp).stem)
        merged.extend(recs)
        per.append({"input": inp, "loaded": len(recs)})
    clean_dir = data / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    name = output_name if output_name.lower().endswith(".json") else output_name + ".json"
    out = clean_dir / name
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_recipe(
        control,
        {
            "op": "merge",
            "inputs": inputs,
            "output": str(out),
            "total": len(merged),
            "per_input": per,
        },
    )
    _mark_cleaning(control)
    return {"output_path": str(out), "total": len(merged), "per_input": per}


def record_cleaning_step(product_id, note, before, after, output_root) -> dict:
    control, _ = resolve_dirs(product_id, output_root)
    _append_recipe(control, {"manual": True, "note": note, "before": before, "after": after})
    _mark_cleaning(control)
    return {"recorded": note}


def register_product(product_id, path, kind, title, notes, output_root) -> dict:
    control, data = resolve_dirs(product_id, output_root)
    p = _rp(data, path)
    exists = p.exists() and p.is_file()
    size = p.stat().st_size if exists else 0
    non_empty = exists and size > 0
    rel = str(p.relative_to(data)) if _under(p, data) else str(p)
    m = ProductManifestFile.load(control / "manifest.yaml")
    m.add_product(
        ProductEntry(kind=kind, path=rel, title=title, bytes=size, non_empty=non_empty, notes=notes)
    )
    if non_empty:
        m.set_dimension(
            "products_produced",
            "pass",
            basis="registered product exists + non-empty",
            evidence=f"{p.name} ({size} B)",
        )
    m.save(control / "manifest.yaml")
    return {
        "registered": rel,
        "kind": kind,
        "bytes": size,
        "non_empty": non_empty,
        "products": len(m.products),
        "outcome": m.outcome,
    }


def update_product_manifest(product_id, dim, status, basis, evidence, output_root) -> dict:
    control, _ = resolve_dirs(product_id, output_root)
    p = control / "manifest.yaml"
    m = ProductManifestFile.load(p)
    m.set_dimension(dim, status, basis=basis or None, evidence=evidence or None)
    m.save(p)
    return {"dim": dim, "status": status, "outcome": m.outcome}


def read_product_manifest(product_id, output_root) -> dict:
    control, _ = resolve_dirs(product_id, output_root)
    m = ProductManifestFile.load(control / "manifest.yaml")
    return {
        "outcome": m.outcome,
        "cleaning_applied": m.cleaning_applied,
        "sources": [s.model_dump() for s in m.sources],
        "products": [p.model_dump() for p in m.products],
        "dimensions": {
            n: {"status": d.status, "gating": d.gating, "evidence": d.evidence}
            for n, d in m.dimensions.items()
        },
    }


def write_product_report(product_id, section, output_root) -> dict:
    control, _ = resolve_dirs(product_id, output_root)
    control.mkdir(parents=True, exist_ok=True)
    rp = control / "report.md"
    prev = rp.read_text(encoding="utf-8") if rp.exists() else "# Data product run report\n\n"
    rp.write_text(prev + section.rstrip() + "\n\n", encoding="utf-8")
    return {"written": "report.md"}


def append_feedback(product_id, claim, basis, priority, notes, output_root) -> dict:
    """Append a data-quality feedback item to products/<id>/feedback.yaml — same
    shape /explore + the validator/run feedback already use. (No SessionStart
    hook surfaces it yet — v1 keeps the channel but defers the auto-wiring.)"""
    control, _ = resolve_dirs(product_id, output_root)
    control.mkdir(parents=True, exist_ok=True)
    fp = control / "feedback.yaml"
    data = yaml.safe_load(fp.read_text(encoding="utf-8")) if fp.exists() else None
    items = data if isinstance(data, list) else []
    items.append({"claim": claim, "basis": basis, "priority": priority, "notes": notes})
    fp.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"appended": claim, "total": len(items)}


# ── server builder (config injects the sizing knobs) ─────────────────


_DEFAULT_CONFIG = {
    "output_root": None,
    "sample_rows": 20,
    "top_n": 15,
    "max_field_chars": 200,
}


def build_data_server(config: dict | None = None):
    """In-process SDK MCP server with the Data Agent's convenience tools.
    ``config`` carries output_root + the profiling sizing knobs (not agent-owned)."""
    cfg = {**_DEFAULT_CONFIG, **(config or {})}
    out_root = cfg["output_root"]

    @tool(
        "list_available_sources",
        "List workspaces that have a crawled dataset ready to consume (latest run / "
        "output_sample), with their originating user_need. Cheap — no record bodies.",
        {},
    )
    @_safe
    async def _t_list(args):
        return _ok(list_available_sources())

    @tool(
        "profile_dataset",
        "Summarize a dataset WITHOUT loading it all into context: per-field coverage, dtypes, "
        "distinct count, top values, numeric range + a small row sample. path is relative to the "
        "product dir (e.g. 'sources/<file>.json' / 'clean/<file>.json') or absolute. ALWAYS profile "
        "before reasoning over a dataset; compute full-data results with code, not by reading rows.",
        _schema(
            {
                "product_id": str,
                "path": str,
                "sample_rows": int,
                "top_n": int,
                "max_field_chars": int,
            },
            ["product_id", "path"],
        ),
    )
    @_safe
    async def _t_profile(args):
        return _ok(
            profile_dataset(
                args["product_id"],
                args["path"],
                out_root,
                args.get("sample_rows") or cfg["sample_rows"],
                args.get("top_n") or cfg["top_n"],
                args.get("max_field_chars") or cfg["max_field_chars"],
            )
        )

    @tool(
        "read_records",
        "Read a window of records (offset/limit) from a dataset with long fields truncated — for "
        "spot-checking, NOT bulk loading. Prefer profile_dataset + code for anything aggregate.",
        _schema(
            {"product_id": str, "path": str, "offset": int, "limit": int, "max_field_chars": int},
            ["product_id", "path"],
        ),
    )
    @_safe
    async def _t_read(args):
        return _ok(
            read_records(
                args["product_id"],
                args["path"],
                out_root,
                args.get("offset") or 0,
                args.get("limit") or 20,
                args.get("max_field_chars") or cfg["max_field_chars"],
            )
        )

    @tool(
        "apply_cleaning",
        "Apply an ORDERED list of cleaning steps the USER specified to a dataset, writing the result "
        "to clean/<output_name> and an auditable cleaning_recipe.yaml (before/after counts). The "
        "staged source is never mutated. steps: each {op, ...params}; ops = dedupe(keys?), "
        "drop_empty(fields, mode=any|all), coerce(field, to=int|float|str|bool), "
        "filter(field, cmp, value) [cmp: ==,!=,>,<,>=,<=,contains,in,notempty,isempty], "
        "normalize_ws(fields?), select(fields), rename(map). "
        "Use this for the user's cleaning instructions; for ops it can't express, write pandas via "
        "Bash and log it with record_cleaning_step.",
        {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "input_path": {"type": "string"},
                "output_name": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "ordered steps, each an object {op, ...params}",
                },
            },
            "required": ["product_id", "input_path", "output_name", "steps"],
        },
    )
    @_safe
    async def _t_clean(args):
        return _ok(
            apply_cleaning(
                args["product_id"], args["input_path"], args["output_name"], args["steps"], out_root
            )
        )

    @tool(
        "merge_datasets",
        "Concatenate several record datasets (cross-source union) into clean/<output_name>, "
        "optionally tagging each row's origin in source_field. inputs are paths relative to the "
        "product dir or absolute. Logged to cleaning_recipe.yaml.",
        {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "inputs": {"type": "array", "items": {"type": "string"}},
                "output_name": {"type": "string"},
                "source_field": {"type": "string"},
            },
            "required": ["product_id", "inputs", "output_name"],
        },
    )
    @_safe
    async def _t_merge(args):
        return _ok(
            merge_datasets(
                args["product_id"],
                args["inputs"],
                args["output_name"],
                args.get("source_field") or "",
                out_root,
            )
        )

    @tool(
        "record_cleaning_step",
        "Log a cleaning step you performed with your own code (pandas via Bash) into "
        "cleaning_recipe.yaml so the audit trail is complete. before/after are record counts.",
        _schema(
            {"product_id": str, "note": str, "before": int, "after": int},
            ["product_id", "note"],
        ),
    )
    @_safe
    async def _t_record(args):
        return _ok(
            record_cleaning_step(
                args["product_id"], args["note"], args.get("before"), args.get("after"), out_root
            )
        )

    @tool(
        "register_product",
        "Register a deliverable you produced (so the manifest indexes it + the completion gate sees "
        "it). path is relative to the product dir (e.g. 'products/report.md') or absolute. "
        "kind: report | dataset | chart | spreadsheet | deck | document | other. A non-empty "
        "registration sets the products_produced gate to pass.",
        _schema(
            {"product_id": str, "path": str, "kind": str, "title": str, "notes": str},
            ["product_id", "path", "kind"],
        ),
    )
    @_safe
    async def _t_register(args):
        return _ok(
            register_product(
                args["product_id"],
                args["path"],
                args["kind"],
                args.get("title") or "",
                args.get("notes") or "",
                out_root,
            )
        )

    @tool(
        "update_product_manifest",
        "Set one completion dimension's status (pass/fail/not_verified/advisory/n/a) + basis + "
        "evidence; returns the recomputed outcome. Most dims are set for you by tool side-effects; "
        "use this to override or to record within_budget. basis/evidence optional.",
        _schema(
            {"product_id": str, "dim": str, "status": str, "basis": str, "evidence": str},
            ["product_id", "dim", "status"],
        ),
    )
    @_safe
    async def _t_update(args):
        return _ok(
            update_product_manifest(
                args["product_id"],
                args["dim"],
                args["status"],
                args.get("basis") or "",
                args.get("evidence") or "",
                out_root,
            )
        )

    @tool(
        "read_product_manifest",
        "Read the current product manifest: sources, registered products, dimensions + computed "
        "outcome. Read this at the end to get the outcome for your final line.",
        {"product_id": str},
    )
    @_safe
    async def _t_read_man(args):
        return _ok(read_product_manifest(args["product_id"], out_root))

    @tool(
        "write_product_report",
        "Append a narrative section to products/<id>/report.md (what you built, sources used, "
        "cleaning applied, the product list, any data-quality caveat).",
        {"product_id": str, "section": str},
    )
    @_safe
    async def _t_report(args):
        return _ok(write_product_report(args["product_id"], args["section"], out_root))

    @tool(
        "append_feedback",
        "Append a data-quality feedback item to products/<id>/feedback.yaml when the data was "
        "inadequate for the requested product (missing field, too sparse, wrong granularity). "
        "basis/priority/notes optional.",
        _schema(
            {"product_id": str, "claim": str, "basis": str, "priority": str, "notes": str},
            ["product_id", "claim"],
        ),
    )
    @_safe
    async def _t_feedback(args):
        return _ok(
            append_feedback(
                args["product_id"],
                args["claim"],
                args.get("basis") or "",
                args.get("priority") or "medium",
                args.get("notes") or "",
                out_root,
            )
        )

    tools = [
        _t_list,
        _t_profile,
        _t_read,
        _t_clean,
        _t_merge,
        _t_record,
        _t_register,
        _t_update,
        _t_read_man,
        _t_report,
        _t_feedback,
    ]
    return create_sdk_mcp_server("data-tools", version="0.1.0", tools=tools)


DATA_TOOL_NAMES = [
    "list_available_sources",
    "profile_dataset",
    "read_records",
    "apply_cleaning",
    "merge_datasets",
    "record_cleaning_step",
    "register_product",
    "update_product_manifest",
    "read_product_manifest",
    "write_product_report",
    "append_feedback",
]


__all__ = ["build_data_server", "DATA_TOOL_NAMES"]
