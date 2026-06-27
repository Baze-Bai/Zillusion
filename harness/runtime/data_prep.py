"""Deterministic core for the Data Agent — pure stdlib logic, no LLM, no deps.

The production analog of ``validator_checks`` / ``run_exec``, but for the
*consumption* stage: it does NOT run a crawler. It resolves the user-selected
crawled datasets, stages read-only copies, profiles them (so the agent reasons
over SUMMARIES + writes code for full-data work, never dumping every record into
context), and provides **user-directed** cleaning primitives that produce an
auditable recipe (before/after counts) while never touching the staged originals.

Stdlib only (json / csv / re / shutil / statistics) so this module has no heavy
dependency — the Data Agent installs pandas/openpyxl/matplotlib/etc. at runtime
itself only when a specific product needs them.

Directory convention (mirrors run_exec's control-on-E / bulky-on-D split):
  - control dir  = ``<root>/products/<product_id>/``        (manifest / recipe / report)
  - data dir     = ``<output_root>/products/<product_id>/`` if --output-root else == control
        sources/  staged read-only source copies
        clean/    cleaned datasets the agent produces
        products/ the actual deliverables
"""

from __future__ import annotations

import csv
import json
import operator as _op
import re
import shutil
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from runtime.validator_checks import PROJECT_ROOT, _unwrap, _workspace


# ── directory layout ─────────────────────────────────────────────────


def resolve_dirs(product_id: str, output_root: str | Path | None = None) -> tuple[Path, Path]:
    """(control_dir, data_dir). control is ALWAYS under <root>/products/ (E:,
    discoverable); data dir holds the bulky sources/clean/products and moves to
    --output-root when given."""
    control = PROJECT_ROOT / "products" / product_id
    data = (Path(output_root).resolve() / "products" / product_id) if output_root else control
    return control, data


# ── source resolution ────────────────────────────────────────────────

_RECORD_EXTS = {".json", ".jsonl", ".ndjson", ".csv", ".tsv"}


def _user_need(site_id: str) -> str | None:
    """The originating 'User need:' line from inputs/<site_id>/goal.md, if any."""
    g = PROJECT_ROOT / "inputs" / site_id / "goal.md"
    if not g.exists():
        return None
    try:
        for line in g.read_text(encoding="utf-8").splitlines():
            if "User need" in line:
                for sep in (":", "："):
                    if sep in line:
                        return line.split(sep, 1)[1].strip() or None
    except OSError:
        return None
    return None


def _latest_run(workspace: Path) -> tuple[str, Path | None, str | None] | None:
    """(run_id, output_path|None, outcome|None) for the workspace's latest run —
    preferring a COMPLETE one, else most-recent by mtime."""
    runs_dir = workspace / "runs"
    if not runs_dir.is_dir():
        return None
    candidates = [d for d in runs_dir.iterdir() if d.is_dir()]
    if not candidates:
        return None

    def outcome_of(d: Path) -> str | None:
        mf = d / "manifest.yaml"
        if not mf.exists():
            return None
        try:
            from mcp_server.schemas import RunManifestFile

            return RunManifestFile.load(mf).outcome
        except Exception:  # noqa: BLE001
            return None

    def mtime(d: Path) -> float:
        try:
            return d.stat().st_mtime
        except OSError:
            return 0.0

    complete = [d for d in candidates if outcome_of(d) == "complete"]
    best = max(complete or candidates, key=mtime)
    out = best / "output.json"
    if not out.exists():
        out = best / "output_sample.json"
    return (best.name, out if out.exists() else None, outcome_of(best))


def resolve_source(token: str) -> dict[str, Any]:
    """Resolve one --sources token to a concrete dataset descriptor.

    token forms: an existing file path; ``site_id``; or ``site_id:run_id``.
    Returns: {token, kind, site_id, run_id, type(records|file), source_path,
    exists, user_need, note}.
    """
    desc: dict[str, Any] = {
        "token": token,
        "kind": None,
        "site_id": None,
        "run_id": None,
        "type": "records",
        "source_path": None,
        "exists": False,
        "user_need": None,
        "note": "",
    }

    p = Path(token)
    if p.exists() and p.is_file():
        desc.update(
            kind="file",
            type=("records" if p.suffix.lower() in _RECORD_EXTS else "file"),
            source_path=str(p.resolve()),
            exists=True,
        )
        return desc

    # site_id[:run_id] — but don't split a Windows drive path (e.g. "D:\x").
    site_id, run_id = token, None
    if ":" in token and not (len(token) >= 2 and token[1] == ":"):
        site_id, run_id = token.split(":", 1)

    ws = _workspace(site_id)
    if not ws.is_dir():
        desc["note"] = f"workspace not found: {site_id}"
        return desc
    desc["site_id"] = site_id
    desc["user_need"] = _user_need(site_id)

    if (ws / "download_manifest.yaml").exists():  # download workflow → file dataset(s)
        rid = run_id or (_latest_run(ws) or (None, None, None))[0]
        d = (ws / "runs" / rid) if rid else ws
        desc.update(
            kind="site",
            run_id=rid,
            type="file",
            source_path=str(d.resolve()),
            exists=Path(d).exists(),
            note="download source — files in this dir; load via code",
        )
        return desc

    if run_id:
        run_dir = ws / "runs" / run_id
        out = run_dir / "output.json"
        if not out.exists():
            out = run_dir / "output_sample.json"
        desc["run_id"] = run_id
        src = out if out.exists() else None
    else:
        lr = _latest_run(ws)
        if lr:
            desc["run_id"] = lr[0]
            src = lr[1]
        else:
            src = None
        if src is None:  # no run yet — fall back to explore's output_sample.json
            osp = ws / "output_sample.json"
            src = osp if osp.exists() else None

    if src is None:
        desc["note"] = "no run output.json / output_sample.json found"
        return desc
    desc.update(kind="site", source_path=str(Path(src).resolve()), exists=True)
    return desc


def _safe_prefix(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", name or "src")


def stage_sources(
    product_id: str, tokens: list[str], output_root: str | Path | None = None
) -> dict[str, Any]:
    """Resolve each token, copy record-datasets read-only into the product's
    ``sources/`` dir, and return descriptors with record_count/field_count/ok.
    Originals are never mutated; file/dir sources are referenced in-place."""
    control, data = resolve_dirs(product_id, output_root)
    sources_dir = data / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    staged: list[dict[str, Any]] = []
    for token in tokens:
        d = resolve_source(token)
        rec_count: int | None = None
        field_count: int | None = None
        staged_path: str | None = None
        ok = False

        if d["exists"] and d["source_path"] and d["type"] == "records":
            recs = load_any(Path(d["source_path"]))
            if recs is not None:
                rec_count = len(recs)
                field_count = len(set().union(*(r.keys() for r in recs))) if recs else 0
                base = Path(d["source_path"]).name
                dest = sources_dir / f"{_safe_prefix(d['site_id'] or 'src')}__{base}"
                try:
                    shutil.copy2(d["source_path"], dest)
                    staged_path = str(dest.relative_to(data)) if _under(dest, data) else str(dest)
                    ok = rec_count > 0
                except OSError as exc:
                    d["note"] = (d["note"] + f" | copy failed: {exc}").strip(" |")
            else:
                d["note"] = (d["note"] + " | could not parse records (json/csv)").strip(" |")
        elif d["exists"] and d["type"] == "file":
            ok = True  # referenced in place (dir / bulky file); agent reads via code
            d["note"] = (d["note"] + f" | in-place: {d['source_path']}").strip(" |")

        staged.append(
            {
                **d,
                "staged_path": staged_path,
                "record_count": rec_count,
                "field_count": field_count,
                "ok": ok,
            }
        )

    return {
        "control_dir": str(control),
        "data_dir": str(data),
        "sources_dir": str(sources_dir),
        "sources": staged,
    }


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ── loading ──────────────────────────────────────────────────────────


def load_any(path: str | Path) -> list[dict] | None:
    """Load a record list from json / jsonl / csv / tsv. Returns None for
    unsupported formats (xlsx/parquet → the agent loads those via code)."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    suf = p.suffix.lower()
    try:
        if suf == ".json":
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            recs = _unwrap(raw)
            return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else None
        if suf in (".jsonl", ".ndjson"):
            out: list[dict] = []
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            return out
        if suf in (".csv", ".tsv"):
            delim = "\t" if suf == ".tsv" else ","
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                return [dict(row) for row in csv.DictReader(f, delimiter=delim)]
    except Exception:  # noqa: BLE001 — bad/partial file → treat as unloadable
        return None
    return None


# ── profiling (summaries, never the whole dataset) ───────────────────


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {} or (isinstance(v, str) and not v.strip())


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _disp(v: Any, maxc: int) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else str(v)
    return s if len(s) <= maxc else s[:maxc] + f"…(+{len(s) - maxc})"


def profile_records(
    records: list[dict], *, sample_rows: int = 20, top_n: int = 15, max_field_chars: int = 200
) -> dict[str, Any]:
    """Per-field summary: coverage, dtype histogram, distinct count, top values,
    numeric range, plus a small row sample. Bounded output — safe for context."""
    dicts = [r for r in records if isinstance(r, dict)]
    n = len(dicts)
    field_union = sorted(set().union(*(r.keys() for r in dicts))) if dicts else []

    fields: dict[str, Any] = {}
    for f in field_union:
        vals = [r[f] for r in dicts if f in r]
        present = len(vals)
        nonnull = sum(1 for v in vals if not _empty(v))
        dtypes: dict[str, int] = {}
        for v in vals:
            dtypes[type(v).__name__] = dtypes.get(type(v).__name__, 0) + 1
        nums = [x for x in (_num(v) for v in vals if not _empty(v)) if x is not None]
        counter = Counter(_disp(v, max_field_chars) for v in vals if not _empty(v))
        entry: dict[str, Any] = {
            "present_in": present,
            "nonnull_in": nonnull,
            "dtypes": dtypes,
            "distinct_capped": len(counter),
            "top_values": [{"value": k, "count": c} for k, c in counter.most_common(top_n)],
        }
        if nums and len(nums) >= max(1, int(present * 0.5)):
            entry["numeric"] = {
                "min": min(nums),
                "max": max(nums),
                "mean": round(statistics.fmean(nums), 4),
            }
        fields[f] = entry

    sample = [{k: _disp(v, max_field_chars) for k, v in r.items()} for r in dicts[:sample_rows]]
    schema_consistent = (len({frozenset(r.keys()) for r in dicts}) <= 1) if dicts else True
    return {
        "record_count": n,
        "field_union": field_union,
        "schema_consistent": schema_consistent,
        "fields": fields,
        "sample_records": sample,
    }


def profile_path(
    path: str | Path, *, sample_rows: int = 20, top_n: int = 15, max_field_chars: int = 200
) -> dict[str, Any]:
    """Profile a dataset file. For unsupported formats, returns basic file info
    so the agent knows to load it via code."""
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "error": "path not found"}
    if p.is_dir():
        files = sorted(str(c.name) for c in p.iterdir() if c.is_file())
        return {"path": str(p), "is_dir": True, "files": files[:200], "file_count": len(files)}
    recs = load_any(p)
    if recs is None:
        return {
            "path": str(p),
            "loadable": False,
            "suffix": p.suffix,
            "bytes": p.stat().st_size,
            "note": "not a json/csv record list — load via code (pandas/openpyxl) to profile",
        }
    prof = profile_records(
        recs, sample_rows=sample_rows, top_n=top_n, max_field_chars=max_field_chars
    )
    return {"path": str(p), "loadable": True, **prof}


# ── user-directed cleaning primitives (auditable) ────────────────────

_COERCE_FAIL = object()


def _hashable(v: Any) -> Any:
    return (
        json.dumps(v, sort_keys=True, default=str, ensure_ascii=False)
        if isinstance(v, (list, dict))
        else v
    )


def _op_dedupe(records: list[dict], keys: list[str] | None) -> list[dict]:
    seen: set = set()
    out = []
    for r in records:
        k = (
            tuple(_hashable(r.get(f)) for f in keys)
            if keys
            else json.dumps(r, sort_keys=True, default=str, ensure_ascii=False)
        )
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _op_drop_empty(records: list[dict], fields: list[str], mode: str) -> list[dict]:
    out = []
    for r in records:
        flags = [_empty(r.get(f)) for f in fields]
        drop = (any(flags) if mode == "any" else all(flags)) if flags else False
        if not drop:
            out.append(r)
    return out


def _coerce_one(v: Any, to: str) -> Any:
    try:
        if to == "int":
            return int(float(str(v).strip().replace(",", "")))
        if to == "float":
            return float(str(v).strip().replace(",", ""))
        if to == "str":
            return str(v)
        if to == "bool":
            return str(v).strip().lower() in ("1", "true", "yes", "y", "t")
    except Exception:  # noqa: BLE001
        return _COERCE_FAIL
    return _COERCE_FAIL


def _op_coerce(records: list[dict], field: str, to: str) -> tuple[list[dict], int]:
    fails = 0
    for r in records:
        if field in r and not _empty(r.get(field)):
            nv = _coerce_one(r[field], to)
            if nv is _COERCE_FAIL:
                fails += 1
            else:
                r[field] = nv
    return records, fails


def _filter_match(v: Any, op: str, value: Any) -> bool:
    if op == "notempty":
        return not _empty(v)
    if op == "isempty":
        return _empty(v)
    if op == "contains":
        return value is not None and str(value) in str(v if v is not None else "")
    if op == "in":
        return v in (value if isinstance(value, list) else [value])
    cmp = {"==": _op.eq, "!=": _op.ne, ">": _op.gt, "<": _op.lt, ">=": _op.ge, "<=": _op.le}
    if op in cmp:
        nv, nval = _num(v), _num(value)
        a, b = (nv, nval) if (nv is not None and nval is not None) else (v, value)
        try:
            return cmp[op](a, b)
        except TypeError:
            return False
    return True


def _op_filter(records: list[dict], field: str, op: str, value: Any) -> list[dict]:
    return [r for r in records if _filter_match(r.get(field), op, value)]


def _op_normalize_ws(records: list[dict], fields: list[str] | None) -> list[dict]:
    for r in records:
        for f in fields if fields else list(r.keys()):
            if isinstance(r.get(f), str):
                r[f] = re.sub(r"\s+", " ", r[f]).strip()
    return records


def _op_select(records: list[dict], fields: list[str]) -> list[dict]:
    return [{k: r[k] for k in fields if k in r} for r in records]


def _op_rename(records: list[dict], mapping: dict[str, str]) -> list[dict]:
    return [{mapping.get(k, k): v for k, v in r.items()} for r in records]


def apply_cleaning_steps(records: list[dict], steps: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply a user-supplied list of cleaning steps in order, returning the
    cleaned records + per-step stats (op / params / before / after / removed).
    Supported ops: dedupe(keys?), drop_empty(fields, mode=any|all),
    coerce(field, to), filter(field, cmp, value), normalize_ws(fields?),
    select(fields), rename(map). Unknown / malformed steps are recorded as an
    error and skipped (never raise — the audit shows what happened)."""
    stats: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            stats.append({"op": None, "error": "step is not an object"})
            continue
        op = step.get("op")
        before = len(records)
        extra: dict[str, Any] = {}
        try:
            if op == "dedupe":
                records = _op_dedupe(records, step.get("keys"))
            elif op == "drop_empty":
                records = _op_drop_empty(records, step.get("fields", []), step.get("mode", "any"))
            elif op == "coerce":
                records, fails = _op_coerce(records, step["field"], step["to"])
                extra["coerce_failures"] = fails
            elif op == "filter":
                # comparator key is `cmp` (NOT `op` — that's the step type; one
                # dict can't hold both under the same key).
                records = _op_filter(
                    records, step["field"], step.get("cmp", "notempty"), step.get("value")
                )
            elif op == "normalize_ws":
                records = _op_normalize_ws(records, step.get("fields"))
            elif op == "select":
                records = _op_select(records, step["fields"])
            elif op == "rename":
                records = _op_rename(records, step["map"])
            else:
                stats.append({"op": op, "error": f"unknown op {op!r}"})
                continue
        except KeyError as exc:
            stats.append({"op": op, "error": f"missing required param {exc}"})
            continue
        after = len(records)
        stats.append(
            {
                "op": op,
                "params": {k: v for k, v in step.items() if k != "op"},
                "before": before,
                "after": after,
                "removed": before - after,
                **extra,
            }
        )
    return records, stats


__all__ = [
    "resolve_dirs",
    "resolve_source",
    "stage_sources",
    "load_any",
    "profile_records",
    "profile_path",
    "apply_cleaning_steps",
]
