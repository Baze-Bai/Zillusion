"""Emit-as-you-go output backend for the Agentic Crawl agent.

The agentic route has NO deterministic workflow.py subprocess — a persistent
SDK agent drives the browser, writes extraction code, and COMMITS records as it
goes until it judges (against the crawl_brief's completeness anchor) that the
harvest is done. This module is that commit substrate, modeled on the discovery
agent's emit-as-you-go pattern (``memory/project_zillusion_emit_as_you_go``):

  - ``records.jsonl`` is the TRUTH — one committed record per line, appended.
  - Tool-side dedup by an identifier field (or a content hash when none is
    declared), so re-committing a unit the agent already scraped is a no-op.
  - ``_tombstones.jsonl`` records deliberately-skipped units WITH a reason
    (an unclearable wall, a 404) so "processed" ≠ "committed" and the
    completeness judgment can account for them.
  - ``_cursor.json`` is the resumable position; a killed crawl restarted with
    the same run_id reloads seen-ids + cursor and continues.
  - ``finalize_crawl`` materializes ``output.json`` from the JSONL (the
    canonical deliverable name the Data Agent consumes) and gate-records the
    completion dimensions.

State lives in a process-level registry (like ``run_exec._ACTIVE_RUNS``) but is
lazily rebuilt from the on-disk JSONL on first touch, so it survives a fresh
process (resume) and never holds the only copy of anything.

Manifest / report / feedback / operator-message writes REUSE the run agent's
backends (``runtime.run_tools``) with ``workflow_type="agentic"`` — no point
duplicating the RunManifestFile plumbing. The browser + human-takeover surface
is NOT here: the agentic agent holds the full browser-harness MCP (like Explore)
and clears walls with those tools directly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from runtime.validator_checks import _workspace, verify_file_refs
from runtime.validator_tools import _ok, _safe, _schema
from runtime.secret_scan import scan_run_secrets
from runtime import run_tools  # reuse manifest/report/feedback/message backends


# ── output paths (always under workspaces/<id>/runs/<run_id>/) ───────


def _run_dir(site_id: str, run_id: str) -> Path:
    return _workspace(site_id) / "runs" / run_id


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ── per-run commit state ─────────────────────────────────────────────


@dataclass
class CrawlState:
    site_id: str
    run_id: str
    run_dir: Path
    identifier_field: str | None
    anchor: dict
    seen: set = field(default_factory=set)
    committed: int = 0
    tombstoned: int = 0
    cursor: Any = None


_CRAWL_STATES: dict[tuple[str, str], CrawlState] = {}


def _key(site_id: str, run_id: str) -> tuple[str, str]:
    return (site_id, run_id)


def _record_key(record: Any, identifier_field: str | None) -> str:
    """Dedup key: the identifier field when present, else a stable content hash
    (so even an undeclared-identifier crawl never double-commits an identical
    record)."""
    if identifier_field and isinstance(record, dict) and record.get(identifier_field) is not None:
        return f"id:{record[identifier_field]}"
    blob = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return "h:" + hashlib.md5(blob.encode("utf-8")).hexdigest()


def _nonempty(v: Any) -> bool:
    """A record field carries data unless it's None / a blank string / an empty
    list or dict. Numbers and bools (incl. 0 / False) ARE data."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def _record_violation(rec: Any) -> str | None:
    """Why this record fails the weak-schema floor, or None if it passes.

    Floor (CLAUDE.md weak-schema minimum): a record is a dict carrying a non-empty
    ``source_url`` plus SOME content — an inline ``content``, a ``file_ref``, or at
    least one other non-empty field (the table-row case where the fields ARE the
    payload). source_url is the provenance handle a sampled live re-fetch needs;
    the carrier guard rejects empty shells (a bare URL with no data)."""
    if not isinstance(rec, dict):
        return f"not a JSON object (got {type(rec).__name__})"
    su = rec.get("source_url")
    if not (isinstance(su, str) and su.strip()):
        return "missing or empty source_url"
    if _nonempty(rec.get("content")) or _nonempty(rec.get("file_ref")):
        return None
    if any(k != "source_url" and _nonempty(v) for k, v in rec.items()):
        return None
    return (
        "no content carrier (need non-empty content, file_ref, or >=1 data field beyond source_url)"
    )


def _load_meta(run_dir: Path) -> dict:
    p = run_dir / "_crawl_meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _get_or_load(site_id: str, run_id: str) -> CrawlState | None:
    """Return the live state, rebuilding it from on-disk JSONL if this process
    hasn't touched this run yet (resume). None only if the run was never
    init_crawl'd (no _crawl_meta.json and no records)."""
    k = _key(site_id, run_id)
    st = _CRAWL_STATES.get(k)
    if st is not None:
        return st

    run_dir = _run_dir(site_id, run_id)
    meta = _load_meta(run_dir)
    records_path = run_dir / "records.jsonl"
    if not meta and not records_path.exists():
        return None  # never initialized

    st = CrawlState(
        site_id=site_id,
        run_id=run_id,
        run_dir=run_dir,
        identifier_field=meta.get("identifier_field"),
        anchor=meta.get("anchor") or {},
    )
    # Rebuild seen-ids + committed count from the truth file (resume).
    if records_path.exists():
        with records_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                st.seen.add(_record_key(rec, st.identifier_field))
                st.committed += 1
    tomb_path = run_dir / "_tombstones.jsonl"
    if tomb_path.exists():
        with tomb_path.open("r", encoding="utf-8") as f:
            st.tombstoned = sum(1 for ln in f if ln.strip())
    cur_path = run_dir / "_cursor.json"
    if cur_path.exists():
        try:
            st.cursor = json.loads(cur_path.read_text(encoding="utf-8")).get("cursor")
        except (OSError, json.JSONDecodeError):
            pass
    _CRAWL_STATES[k] = st
    return st


# ── backend operations ───────────────────────────────────────────────


def init_crawl(
    site_id: str,
    run_id: str,
    identifier_field: str | None = None,
    anchor: dict | None = None,
    data_definition: dict | None = None,
) -> dict:
    """Begin (or resume) an agentic crawl. Writes _crawl_meta.json (identifier +
    completeness anchor) and a fresh agentic manifest with launched=pass. Safe to
    call again with the same run_id: it resumes (reloads seen-ids), preserving
    already-committed records. ``data_definition`` (field/unit name -> meaning)
    lands in the manifest — the declaration downstream consumers read for
    weak-schema records (documents / media / heterogeneous units)."""
    run_dir = _run_dir(site_id, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    anchor = anchor or {}
    _atomic_write(
        run_dir / "_crawl_meta.json",
        json.dumps(
            {"identifier_field": identifier_field, "anchor": anchor}, ensure_ascii=False, indent=2
        ),
    )
    # Drop any stale in-process state, then (re)load from disk so a resume picks
    # up prior records.jsonl.
    _CRAWL_STATES.pop(_key(site_id, run_id), None)
    st = _get_or_load(site_id, run_id)
    if st is None:  # brand-new run: meta exists now, so this shouldn't happen
        st = CrawlState(site_id, run_id, run_dir, identifier_field, anchor)
        _CRAWL_STATES[_key(site_id, run_id)] = st

    run_tools.init_run_manifest(
        site_id,
        run_id,
        workflow_type="agentic",
        crawl_mode="agentic",
        data_definition=data_definition or None,
    )
    run_tools.update_run_manifest(
        site_id,
        run_id,
        "launched",
        "pass",
        basis="agentic crawl session started + bound to workspace",
    )
    return {
        "initialized": True,
        "run_id": run_id,
        "resumed": st.committed > 0 or st.tombstoned > 0,
        "already_committed": st.committed,
        "already_tombstoned": st.tombstoned,
        "cursor": st.cursor,
        "identifier_field": identifier_field,
        "anchor_hardness": anchor.get("hardness"),
    }


def commit_records(site_id: str, run_id: str, records: list) -> dict:
    """Append new records to records.jsonl, deduped by identifier (or content
    hash). Returns committed/duplicate counts + running total. Idempotent:
    re-committing seen units is a no-op (so a retried batch never doubles).

    ENFORCES the weak-schema floor per record (see _record_violation): each must
    be a dict carrying a non-empty source_url + a content carrier. Violators are
    NOT committed; they come back in ``rejected_detail`` (index + reason) so the
    agent can fix its extraction and re-commit — valid records in the same batch
    still land."""
    st = _get_or_load(site_id, run_id)
    if st is None:
        return {"error": "crawl not initialized — call init_crawl first"}
    if not isinstance(records, list):
        return {"error": "records must be a list of objects"}

    fresh, dupes, rejected = [], 0, []
    for idx, rec in enumerate(records):
        problem = _record_violation(rec)
        if problem is not None:
            rejected.append({"index": idx, "reason": problem})
            continue
        k = _record_key(rec, st.identifier_field)
        if k in st.seen:
            dupes += 1
            continue
        st.seen.add(k)
        fresh.append(rec)

    if fresh:
        # Append-only truth file: each record one line. A kill mid-append loses
        # at most the in-flight line, never the prior ones.
        with (st.run_dir / "records.jsonl").open("a", encoding="utf-8") as f:
            for rec in fresh:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        st.committed += len(fresh)

    out = {
        "committed": len(fresh),
        "duplicates": dupes,
        "total_committed": st.committed,
        "total_tombstoned": st.tombstoned,
    }
    if rejected:
        out["rejected"] = len(rejected)
        out["rejected_detail"] = rejected[:20]  # cap so a bad batch can't flood
    return out


def record_skip(site_id: str, run_id: str, unit_id: str, reason: str) -> dict:
    """Tomb-stone a deliberately-skipped unit (unclearable wall, 404, off-goal).
    Counts toward 'processed' but not 'committed' — so the completeness judgment
    can say '23000 scraped + 2104 skipped-with-reason = full set accounted for'
    rather than silently under-counting."""
    st = _get_or_load(site_id, run_id)
    if st is None:
        return {"error": "crawl not initialized — call init_crawl first"}
    rec = {"unit_id": unit_id, "reason": (reason or "").strip() or "unspecified"}
    with (st.run_dir / "_tombstones.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    st.tombstoned += 1
    return {"tombstoned": unit_id, "total_tombstoned": st.tombstoned}


def mark_cursor(site_id: str, run_id: str, cursor: Any) -> dict:
    """Persist the resumable position (a page number, an offset, a list index —
    whatever the crawl's enumeration uses). A resume reloads this."""
    st = _get_or_load(site_id, run_id)
    if st is None:
        return {"error": "crawl not initialized — call init_crawl first"}
    st.cursor = cursor
    _atomic_write(st.run_dir / "_cursor.json", json.dumps({"cursor": cursor}, ensure_ascii=False))
    return {"cursor": cursor}


def get_crawl_progress(site_id: str, run_id: str) -> dict:
    """Live progress vs the completeness anchor. For a HARD anchor with an
    estimated_total, returns a precise ratio; for a SOFT anchor, only the raw
    counts (the agent's stop heuristic governs)."""
    st = _get_or_load(site_id, run_id)
    if st is None:
        return {"error": "crawl not initialized — call init_crawl first"}
    anchor = st.anchor or {}
    est = anchor.get("estimated_total")
    processed = st.committed + st.tombstoned
    ratio = None
    if isinstance(est, int) and est > 0:
        ratio = round(processed / est, 4)
    return {
        "committed": st.committed,
        "tombstoned": st.tombstoned,
        "processed": processed,
        "cursor": st.cursor,
        "anchor_hardness": anchor.get("hardness"),
        "estimated_total": est,
        "progress_ratio": ratio,
        "anchor": anchor,
    }


def check_committed(site_id: str, run_id: str, ids: list) -> dict:
    """Given candidate identifier values, return which are already committed —
    so the agent can skip re-scraping them (cheap resume / dedup at the planning
    layer, before spending a fetch)."""
    st = _get_or_load(site_id, run_id)
    if st is None:
        return {"error": "crawl not initialized — call init_crawl first"}
    done = [i for i in (ids or []) if f"id:{i}" in st.seen]
    todo = [i for i in (ids or []) if f"id:{i}" not in st.seen]
    return {"already_committed": done, "remaining": todo, "remaining_count": len(todo)}


def _read_records(st: CrawlState) -> list:
    """Parse the records.jsonl truth file into a list (skipping blank / corrupt
    lines). The append-only source for both materialization and the
    self-consistency check."""
    records: list = []
    rp = st.run_dir / "records.jsonl"
    if rp.exists():
        with rp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _materialize_output(st: CrawlState) -> tuple[int, Path]:
    """Read the records.jsonl truth and (atomically) write the canonical
    output.json the Data Agent consumes. records.jsonl is kept as the
    append-only source. Returns (record_count, output_path)."""
    records = _read_records(st)
    out_path = st.run_dir / "output.json"
    _atomic_write(out_path, json.dumps(records, ensure_ascii=False, indent=2, default=str))
    return len(records), out_path


def _load_field_schema(site_id: str) -> set[str]:
    """Declared field names from the crawl_brief handoff (workspaces/<id>/
    crawl_brief.yaml — written by write_crawl_brief.save_yaml). Empty set when
    there is no brief or no field_schema, in which case the declared-vs-produced
    diff is simply skipped (never_appeared stays empty)."""
    p = _workspace(site_id) / "crawl_brief.yaml"
    if not p.exists():
        return set()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return set()
    fs = data.get("field_schema") if isinstance(data, dict) else None
    return set(fs.keys()) if isinstance(fs, dict) else set()


def _check_self_consistency(site_id: str, st: CrawlState) -> dict:
    """Internal cross-artifact agreement for the agentic deliverable — purely
    reproduction-free (no live page, no re-run). Compares the crawl_brief's
    declared field_schema keys to the produced record keys (UNION, not [0]),
    backstops the source_url + content-carrier floor on the committed records, and
    verifies file_ref targets exist + are non-empty. MECHANICAL (no LLM): only an
    OBJECTIVE structural break gates a fail; declared-vs-produced drift is reported
    as advisory info (bug-vs-sparse needs judgment the agentic route has no LLM
    verifier for)."""
    records = _read_records(st)
    dicts = [r for r in records if isinstance(r, dict)]
    declared = _load_field_schema(site_id)
    actual = set().union(*(r.keys() for r in dicts)) if dicts else set()
    floor_violations = sum(1 for r in records if _record_violation(r) is not None)

    fr = verify_file_refs(site_id, str(st.run_dir / "output.json"))
    fr_broken = len(
        (fr.get("missing") or []) + (fr.get("empty") or []) + (fr.get("violations") or [])
    )
    return {
        "record_count": len(records),
        "declared": sorted(declared),
        "actual_union": sorted(actual),
        "never_appeared": sorted(declared - actual),  # declared but never produced
        "undeclared": sorted(actual - declared),  # produced but never declared (advisory)
        "schema_consistent": (len({frozenset(r.keys()) for r in dicts}) <= 1) if dicts else True,
        "floor_violations": floor_violations,
        "file_ref_broken": fr_broken,
    }


def abort_crawl(site_id: str, run_id: str, reason: str) -> dict:
    """Driver-side kill finalize (progress-stall / wall-clock) — NOT agent-called.
    Salvages whatever was committed: materializes output.json, sets produced_output
    mechanically + within_budget=fail (so the gate yields PARTIAL with data /
    ABORTED without). completeness stays not_verified (the agent never judged it)."""
    st = _get_or_load(site_id, run_id)
    if st is None:
        return {"aborted": False, "error": "crawl not initialized"}
    rc, out_path = _materialize_output(st)
    run_tools.update_run_manifest(
        site_id,
        run_id,
        "produced_output",
        "pass" if rc > 0 else "fail",
        basis=f"salvaged on kill: {rc} records committed before abort",
        record_count=rc,
        output_path=str(out_path),
    )
    run_tools.update_run_manifest(
        site_id, run_id, "within_budget", "fail", basis=(reason or "").strip() or "killed"
    )
    man = run_tools.read_run_manifest(site_id, run_id)
    return {
        "aborted": True,
        "record_count": rc,
        "output_path": str(out_path),
        "outcome": man.get("outcome"),
    }


def finalize_crawl(
    site_id: str,
    run_id: str,
    completeness_status: str,
    completeness_basis: str,
    reason: str = "",
) -> dict:
    """Close the harvest: materialize output.json from records.jsonl, then
    gate-record the completion dimensions and return the computed outcome.

    - produced_output is set MECHANICALLY (records.jsonl non-empty record list).
    - completeness is the AGENT's anchor-grounded judgment (status + basis) —
      this is the soft-completion call you own; justify it against the anchor.
    - within_budget is set pass here only if it isn't already fail (a driver
      progress-stall / wall-clock kill writes fail before the agent can finalize).
    """
    st = _get_or_load(site_id, run_id)
    if st is None:
        return {"error": "crawl not initialized — call init_crawl first"}

    rc, out_path = _materialize_output(st)

    # produced_output — mechanical.
    run_tools.update_run_manifest(
        site_id,
        run_id,
        "produced_output",
        "pass" if rc > 0 else "fail",
        basis=f"records.jsonl materialized to output.json ({rc} records)",
        record_count=rc,
        output_path=str(out_path),
    )
    # self_consistency — mechanical internal cross-artifact agreement (no LLM):
    # source_url + carrier floor on committed records, declared-vs-produced field
    # drift (advisory), and file_ref existence. Only an objective structural break
    # gates a fail.
    sc = _check_self_consistency(site_id, st)
    if rc == 0:
        sc_status, sc_basis = "n/a", "no records to check (produced_output covers emptiness)"
    elif sc["floor_violations"] or sc["file_ref_broken"]:
        sc_status = "fail"
        sc_basis = (
            f"{sc['floor_violations']} record(s) miss source_url/carrier; "
            f"{sc['file_ref_broken']} file_ref target(s) missing/empty/invalid"
        )
    else:
        sc_status = "pass"
        sc_basis = (
            f"{len(sc['actual_union'])} field(s) produced; "
            f"never_appeared={sc['never_appeared']}; undeclared={sc['undeclared']}; "
            f"schema_consistent={sc['schema_consistent']}"
        )
    run_tools.update_run_manifest(site_id, run_id, "self_consistency", sc_status, basis=sc_basis)

    # secrets_safe — scan shipped artifacts for auth_state.json session-secret
    # literals (cookies / localStorage tokens). Mechanical + hard-floored in
    # update_run_manifest; n/a when the run used no takeover auth.
    ss = scan_run_secrets(_run_dir(site_id, run_id))
    if ss.get("na"):
        ss_status = "n/a"
        ss_basis = ss.get("reason") or "no takeover auth"
    elif ss.get("ok"):
        ss_status = "pass"
        ss_basis = (
            f"scanned {len(ss.get('scanned') or [])} artifact(s); no session-secret literal found"
        )
    else:
        ss_status = "fail"
        files = ", ".join(sorted({leak["file"] for leak in ss.get("leaks") or []}))
        ss_basis = f"session-secret literal leaked into: {files}"
    run_tools.update_run_manifest(site_id, run_id, "secrets_safe", ss_status, basis=ss_basis)

    # completeness — the agent's judgment, with a mechanical backstop.
    cs = (completeness_status or "").strip().lower()
    if cs not in ("pass", "fail", "not_verified", "advisory", "n/a"):
        cs = "not_verified"
    basis = completeness_basis or ""
    # Hard-anchor backstop: when the brief declared a HARD anchor with a concrete
    # estimated_total, "pass" is mechanically checkable — refuse a self-attested
    # pass that did NOT cover the set (the 8000/25104 silent-under-harvest case).
    # A 2% shortfall is tolerated (estimated_total is itself an estimate). Soft
    # anchors stay agent-judged. Also reject a bare "pass" with no justification.
    anchor = st.anchor or {}
    if cs == "pass":
        est = anchor.get("estimated_total")
        processed = st.committed + st.tombstoned
        if (
            anchor.get("hardness") == "hard"
            and isinstance(est, int)
            and est > 0
            and processed < est * 0.98
        ):
            cs = "not_verified"
            basis = (
                f"DOWNGRADED from pass: hard anchor estimated_total={est} but only "
                f"{processed} processed ({processed / est:.0%}) — cursor did not cover "
                f"the full set. Original basis: {completeness_basis or '(none)'}"
            )
        elif not basis.strip():
            cs = "not_verified"
            basis = "DOWNGRADED from pass: no completeness basis given (a pass must justify done against the anchor)"
    run_tools.update_run_manifest(
        site_id,
        run_id,
        "completeness",
        cs,
        basis=basis or "(no basis given)",
    )
    # within_budget — pass unless a kill already set it fail.
    man = run_tools.read_run_manifest(site_id, run_id)
    wb = (man.get("dimensions", {}).get("within_budget") or {}).get("status")
    if wb != "fail":
        run_tools.update_run_manifest(
            site_id,
            run_id,
            "within_budget",
            "pass",
            basis="crawl loop ended on the agent's own judgment (not killed)",
        )
    # partial_failures — advisory tombstone rate.
    if st.tombstoned:
        denom = st.committed + st.tombstoned
        run_tools.update_run_manifest(
            site_id,
            run_id,
            "partial_failures",
            "advisory",
            basis=f"{st.tombstoned}/{denom} units tomb-stoned (skipped with reason)",
        )

    final = run_tools.read_run_manifest(site_id, run_id)
    return {
        "finalized": True,
        "record_count": rc,
        "tombstoned": st.tombstoned,
        "output_path": str(out_path),
        "outcome": final.get("outcome"),
        "reason": (reason or "").strip() or None,
    }


# ── MCP server (the agentic agent's narrow output surface) ───────────


# Tool names the agentic agent is allowed to call from this in-process server,
# alongside the full browser-harness MCP (granted separately) + Read.
EMIT_TOOL_NAMES = [
    "init_crawl",
    "commit_records",
    "record_skip",
    "mark_cursor",
    "get_crawl_progress",
    "check_committed",
    "finalize_crawl",
    "update_run_manifest",
    "read_run_manifest",
    "write_run_report",
    "append_feedback",
    "send_user_message",
]


def build_emit_server(config: dict | None = None):
    """In-process SDK MCP server: the agentic crawl's output/record surface.
    Commit + progress tools are local; manifest/report/feedback/message tools
    reuse the run agent's backends with workflow_type=agentic. ``config`` is
    reserved (output_root etc.) and currently unused."""
    _ = config

    @tool(
        "init_crawl",
        "Begin (or RESUME) the agentic crawl: declare the identifier_field used for dedup and "
        "the completeness_anchor from the crawl_brief (MUST include hardness: 'hard'|'soft', and "
        "estimated_total when hard). Writes the agentic manifest (launched=pass). Calling again "
        "with the same run_id resumes — already-committed records are preserved. "
        "data_definition (field name -> meaning) is REQUIRED when records are weak-schema data "
        "units (documents / media / heterogeneous pages) — carry the brief's field_schema and "
        "refine it to what you actually commit; it lands in the manifest for downstream readers.",
        _schema(
            {
                "site_id": str,
                "run_id": str,
                "identifier_field": str,
                "anchor": dict,
                "data_definition": dict,
            },
            ["site_id", "run_id"],
        ),
    )
    @_safe
    async def _t_init(args):
        return _ok(
            init_crawl(
                args["site_id"],
                args["run_id"],
                identifier_field=args.get("identifier_field") or None,
                anchor=args.get("anchor") or {},
                data_definition=args.get("data_definition") or None,
            )
        )

    @tool(
        "commit_records",
        "Append a BATCH of scraped records to records.jsonl (the truth file), deduped by "
        "identifier (or content hash). Call this AS YOU GO — never hold the only copy of scraped "
        "data in memory. EACH record MUST be an object with a non-empty source_url (its "
        "provenance URL) plus a content carrier — inline `content`, a `file_ref`, or >=1 other "
        "data field. Records that don't are NOT committed and come back in `rejected_detail` "
        "(index + reason); fix the extraction and re-commit them (valid records in the batch "
        "still land). Returns committed/duplicate/rejected counts + running total. Idempotent: a "
        "re-committed batch is a no-op, so retrying after an error is safe.",
        _schema({"site_id": str, "run_id": str, "records": list}, ["site_id", "run_id", "records"]),
    )
    @_safe
    async def _t_commit(args):
        return _ok(commit_records(args["site_id"], args["run_id"], args.get("records") or []))

    @tool(
        "record_skip",
        "Tomb-stone a unit you DELIBERATELY skip (unclearable wall, 404, off-goal) WITH a reason. "
        "Keeps 'processed' honest vs 'committed' so your completeness judgment can account for "
        "every unit. Don't tomb-stone transient failures you'll retry — only final skips.",
        {"site_id": str, "run_id": str, "unit_id": str, "reason": str},
    )
    @_safe
    async def _t_skip(args):
        return _ok(
            record_skip(args["site_id"], args["run_id"], args["unit_id"], args.get("reason") or "")
        )

    @tool(
        "mark_cursor",
        "Persist your resumable position (page number, offset, list index — whatever your "
        "enumeration uses). A killed crawl restarted with the same run_id reloads this. Call it "
        "each time you advance a batch.",
        _schema({"site_id": str, "run_id": str, "cursor": Any}, ["site_id", "run_id", "cursor"]),
    )
    @_safe
    async def _t_cursor(args):
        return _ok(mark_cursor(args["site_id"], args["run_id"], args.get("cursor")))

    @tool(
        "get_crawl_progress",
        "Live progress vs the completeness anchor: committed, tombstoned, processed, cursor, and "
        "(for a HARD anchor) progress_ratio against estimated_total. Use it to decide whether "
        "you've reached the anchor before finalize_crawl.",
        {"site_id": str, "run_id": str},
    )
    @_safe
    async def _t_progress(args):
        return _ok(get_crawl_progress(args["site_id"], args["run_id"]))

    @tool(
        "check_committed",
        "Given candidate identifier values, return which are ALREADY committed (and which remain) "
        "— skip re-scraping the done ones before spending a fetch. Cheap resume + planning-layer "
        "dedup.",
        _schema({"site_id": str, "run_id": str, "ids": list}, ["site_id", "run_id", "ids"]),
    )
    @_safe
    async def _t_check(args):
        return _ok(check_committed(args["site_id"], args["run_id"], args.get("ids") or []))

    @tool(
        "finalize_crawl",
        "Close the harvest when you judge it complete against the anchor. Materializes output.json "
        "from records.jsonl, sets produced_output (mechanical) + completeness (YOUR judgment: "
        "status pass/fail + basis citing the anchor — hard: cursor reached full set, every unit "
        "scraped or tomb-stoned; soft: stop heuristic met + why) + within_budget, and returns the "
        "gate-computed outcome. Then emit the outcome line.",
        _schema(
            {
                "site_id": str,
                "run_id": str,
                "completeness_status": str,
                "completeness_basis": str,
                "reason": str,
            },
            ["site_id", "run_id", "completeness_status", "completeness_basis"],
        ),
    )
    @_safe
    async def _t_finalize(args):
        return _ok(
            finalize_crawl(
                args["site_id"],
                args["run_id"],
                args["completeness_status"],
                args["completeness_basis"],
                reason=args.get("reason") or "",
            )
        )

    # Manifest / report / feedback / operator-message — reuse run agent backends.
    @tool(
        "update_run_manifest",
        "Set one completion dimension's status (pass/fail/not_verified/advisory/n/a) + basis + "
        "evidence. Dimensions: launched, produced_output, completeness, within_budget (gating); "
        "partial_failures (advisory). Usually you only touch these via init_crawl/finalize_crawl, "
        "but use this to add evidence or correct a dimension.",
        _schema(
            {
                "site_id": str,
                "run_id": str,
                "dim": str,
                "status": str,
                "basis": str,
                "evidence": str,
                "record_count": int,
                "output_path": str,
            },
            ["site_id", "run_id", "dim", "status"],
        ),
    )
    @_safe
    async def _t_update(args):
        rc = args.get("record_count")
        return _ok(
            run_tools.update_run_manifest(
                args["site_id"],
                args["run_id"],
                args["dim"],
                args["status"],
                basis=args.get("basis") or "",
                evidence=args.get("evidence") or "",
                record_count=rc if isinstance(rc, int) else None,
                output_path=args.get("output_path") or "",
            )
        )

    @tool(
        "read_run_manifest",
        "Read the current manifest state + computed outcome.",
        {"site_id": str, "run_id": str},
    )
    @_safe
    async def _t_read_man(args):
        return _ok(run_tools.read_run_manifest(args["site_id"], args["run_id"]))

    @tool(
        "write_run_report",
        "Append a narrative section to runs/<run_id>/report.md (what ran, yield, anomalies, how "
        "the anchor was reached).",
        {"site_id": str, "run_id": str, "section": str},
    )
    @_safe
    async def _t_report(args):
        return _ok(run_tools.write_run_report(args["site_id"], args["run_id"], args["section"]))

    @tool(
        "append_feedback",
        "Append a hypothesis-feedback item to runs/<run_id>/feedback.yaml — for full-scale "
        "problems the harvest revealed (deep template drift, scale anti-bot, an anchor that "
        "turned out wrong). The next /explore reads it. basis/priority/notes optional.",
        _schema(
            {
                "site_id": str,
                "run_id": str,
                "claim": str,
                "basis": str,
                "priority": str,
                "notes": str,
            },
            ["site_id", "run_id", "claim"],
        ),
    )
    @_safe
    async def _t_feedback(args):
        return _ok(
            run_tools.append_feedback(
                args["site_id"],
                args["run_id"],
                args["claim"],
                basis=args.get("basis") or "",
                priority=args.get("priority") or "medium",
                notes=args.get("notes") or "",
            )
        )

    @tool(
        "send_user_message",
        "Send a short message to the USER watching this crawl — appears as a chat bubble. Use it "
        "to answer their mid-crawl guidance, flag an anomaly, or report a milestone. Your plain "
        "assistant text is NOT shown to them; this is the only channel. Write in their language; "
        "keep it brief.",
        {"site_id": str, "run_id": str, "message": str},
    )
    @_safe
    async def _t_msg(args):
        return _ok(run_tools.send_user_message(args["site_id"], args["run_id"], args["message"]))

    tools = [
        _t_init,
        _t_commit,
        _t_skip,
        _t_cursor,
        _t_progress,
        _t_check,
        _t_finalize,
        _t_update,
        _t_read_man,
        _t_report,
        _t_feedback,
        _t_msg,
    ]
    return create_sdk_mcp_server("crawl-emit", version="0.1.0", tools=tools)


__all__ = [
    "build_emit_server",
    "EMIT_TOOL_NAMES",
    "init_crawl",
    "commit_records",
    "record_skip",
    "mark_cursor",
    "get_crawl_progress",
    "check_committed",
    "finalize_crawl",
    "abort_crawl",
    "CrawlState",
]
