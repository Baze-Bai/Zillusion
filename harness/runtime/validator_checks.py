"""Deterministic validator checks — pure logic, no LLM, no playwright.

These back the gating dimensions that don't need a browser:
  - check_workflow_syntax  -> dimension `syntax`
  - run_workflow_isolated  -> dimension `runs_clean` (+ feeds reproducibility)
  - compare_field_names    -> dimension `self_consistency` (field-name part)

**Isolation invariant** (user mandate 2026-05-29): the validator NEVER
writes an explore artifact. `run_workflow_isolated` runs workflow.py in a
copied workdir under `validation/<run_id>/rerun/`, so its
`output_sample.json` lands THERE — never overwriting explore's.

These are plain functions; the `@tool` / `create_sdk_mcp_server` wrapper
that exposes them to the validator SDK agent lives in
`runtime/validator_tools.py`. Keeping the logic separate makes it
unit-testable without the SDK.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from runtime.subprocess_safety import scrubbed_env

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Evidence-size knobs (operator-approved 2026-06-11, weak-schema support):
# page-sized `content` values must not blow up the agent's context, and a
# few-thousand-file media crawl must not turn a listing into a huge JSON.
# Clipping affects EVIDENCE ONLY — every comparison runs on full values.
# The string branch of _within_tolerance is the one knob that changes a
# verdict: TOLERANT long text counts as within when length drift <= 5%
# AND the opening _STR_PREFIX chars are identical.
_CLIP_CHARS = 200
_STR_PREFIX = 64
_STR_LEN_TOL = 0.05
_LIST_CAP = 200


def _workspace(site_id: str) -> Path:
    return PROJECT_ROOT / "workspaces" / site_id


def resolve_auth_path() -> Path:
    """auth_state.json for the run's OWNING user — MUST mirror the MCP server's
    _user_auth_path so the cookies saved by the explore-phase takeover are the
    ones validate + run load. Per-user (workspaces/_auth/<uid>/auth_state.json)
    when CRAWLER_EXPLORER_USER_ID is set (multi-tenant prod); the shared
    project-global auth_state.json otherwise (dev/local). The id is reduced to
    ONE filesystem-safe segment so a hostile id can't escape the _auth root."""
    uid = os.environ.get("CRAWLER_EXPLORER_USER_ID", "").strip()
    if not uid:
        return PROJECT_ROOT / "auth_state.json"
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in uid)[:128] or "_"
    return PROJECT_ROOT / "workspaces" / "_auth" / safe / "auth_state.json"


def _clip(v):
    """Bound one evidence value for a tool result. Strings keep their head;
    big lists/dicts degrade to a clipped repr; everything else passes through."""
    if isinstance(v, str):
        return v if len(v) <= _CLIP_CHARS else v[:_CLIP_CHARS] + f"…(+{len(v) - _CLIP_CHARS} chars)"
    if isinstance(v, (list, dict)):
        s = repr(v)
        return v if len(s) <= _CLIP_CHARS else s[:_CLIP_CHARS] + f"…(+{len(s) - _CLIP_CHARS} chars)"
    return v


# ── dimension: syntax ────────────────────────────────────────────────


def check_workflow_syntax(site_id: str) -> dict:
    """`ast.parse` workflow.py. Pure, deterministic. Reads source only —
    this is the ONE legitimate static read of workflow.py (Python syntax
    is standardized; everything else judges output, not source)."""
    path = _workspace(site_id) / "workflow.py"
    if not path.exists():
        return {"ok": False, "errors": [{"line": None, "msg": "workflow.py not found"}]}
    try:
        ast.parse(path.read_text(encoding="utf-8"))
        return {"ok": True, "errors": []}
    except SyntaxError as e:
        return {
            "ok": False,
            "errors": [{"line": e.lineno, "msg": e.msg, "text": (e.text or "").strip()}],
        }


# ── dimension: runs_clean (+ feeds reproducibility) ──────────────────

# Files run_workflow_isolated copies INTO rerun/ — not workflow products.
_STAGED_INPUTS = {
    "workflow.py",
    "helpers.py",
    "selectors.yaml",
    "api_manifest.yaml",
    "credentials.json",
    "auth_state.json",
}


def _stray_output_samples(rerun: Path) -> list[str]:
    """output_sample.json files BELOW rerun/ when the root has none — an
    ADVISORY output-location signal (workflow.py SHOULD write at its cwd root).
    Empty list when the root copy exists or nothing strayed. Surfaced ONLY by
    run_workflow_isolated as an agent-visible signal the validator WEIGHS for
    runs_clean; it is NOT floored (the safety floor fires only on a crash — see
    _floor_violation), so the agent decides."""
    try:
        if (rerun / "output_sample.json").is_file():  # a DIRECTORY of this name is not a root copy
            return []
        out = []
        for p in rerun.rglob("output_sample.json"):
            rel = p.relative_to(rerun)
            if any(part.startswith("_") for part in rel.parts):
                continue
            if p.is_file():
                out.append(rel.as_posix())
        return sorted(out)
    except OSError:
        return []  # unreadable tree → no claim either way


def _list_produced(rerun: Path) -> dict:
    """Everything workflow.py actually WROTE into rerun/ — the agent's only
    visibility into non-JSON deliverables (media/, weak-schema file_ref
    targets) and into contract drift (output written to the wrong spot).
    Excludes the staged inputs (top level), control files (any path part
    starting with `_`, e.g. _last_run_status.json) and *.tmp partials."""
    files: list[dict] = []
    for p in sorted(rerun.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(rerun)
        if any(part.startswith("_") for part in rel.parts):
            continue
        if p.suffix.lower() == ".tmp":
            continue
        if len(rel.parts) == 1 and rel.parts[0] in _STAGED_INPUTS:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        files.append({"path": rel.as_posix(), "bytes": size})
    return {
        "produced_files": files[:_LIST_CAP],
        "produced_files_total": len(files),
        "produced_files_truncated": len(files) > _LIST_CAP,
    }


def _persist_run_result(
    rerun: Path, *, exit_code: int | None, timed_out: bool, traceback: bool
) -> None:
    """Persist the rerun outcome to rerun/_run_result.json so the runs_clean
    FLOOR (update_scorecard) can re-read a CRASH from DISK rather than trust a
    relayed tool result. crashed = a non-zero exit OR a Python traceback while
    NOT a timeout — the only runs_clean condition the validator agent may never
    wave to pass. A timeout or an output-location issue is NOT a crash — those
    are advisory (the agent judges them)."""
    crashed = (not timed_out) and (exit_code not in (0, None) or traceback)
    try:
        (rerun / "_run_result.json").write_text(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "traceback": traceback,
                    "crashed": crashed,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def run_workflow_isolated(
    site_id: str,
    run_id: str,
    *,
    timeout_s: int = 300,
    python_exe: str | None = None,
) -> dict:
    """Run workflow.py in an ISOLATED workdir so it can NEVER overwrite
    explore's output_sample.json.

    Copies workflow.py + helpers.py + selectors.yaml into
    `validation/<run_id>/rerun/` and runs with cwd there, so all of
    workflow.py's outputs (output_sample.json / media/ / _last_run_status)
    land in rerun/. Explore's workspace files are read-only throughout.

    Returns run facts + the isolated output path + explore's output path
    (so check 4 / compare_output can diff the two — which needs both)."""
    ws = _workspace(site_id)
    wf = ws / "workflow.py"
    if not wf.exists():
        return {"ran": False, "reason": "workflow.py not found"}

    rerun = ws / "validation" / run_id / "rerun"
    rerun.mkdir(parents=True, exist_ok=True)
    # Copy only the inputs workflow.py needs; explore artifacts stay untouched.
    for fname in ("workflow.py", "helpers.py", "selectors.yaml", "api_manifest.yaml"):
        src = ws / fname
        if src.exists():
            shutil.copy2(src, rerun / fname)
    # Stage the owning user's auth_state.json into rerun/ so workflow.py's walk-up
    # from __file__ finds it in cwd. Under multi-tenant prod the cookies live at
    # workspaces/_auth/<uid>/auth_state.json (resolve_auth_path) — NOT on rerun/'s
    # parent chain — so without this local copy a login-gated workflow validates
    # UNauthenticated (public data only). Mirrors credentials.json below.
    auth_src = resolve_auth_path()
    if auth_src.exists():
        shutil.copy2(auth_src, rerun / "auth_state.json")
    # api workflows: the user-supplied key. inputs/<site>/ is NOT on rerun/'s
    # parent chain, so the walk-up in workflow.py only finds it via this local copy.
    creds = PROJECT_ROOT / "inputs" / site_id / "credentials.json"
    if creds.exists():
        shutil.copy2(creds, rerun / "credentials.json")

    py = python_exe or sys.executable
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [py, "workflow.py"],
            cwd=rerun,  # ← key: workflow.py writes output_sample.json HERE
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            # workflow.py is agent-written + untrusted: never hand it the
            # validator's secrets. The per-site credential (if any) is staged
            # as credentials.json in cwd, not read from env. See subprocess_safety.
            env=scrubbed_env(),
        )
        iso_out = rerun / "output_sample.json"
        _persist_run_result(
            rerun,
            exit_code=r.returncode,
            timed_out=False,
            traceback="Traceback (most recent call last)" in (r.stderr or ""),
        )
        return {
            "ran": True,
            "exit_code": r.returncode,
            "timed_out": False,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
            "stderr_tail": (r.stderr or "")[-2000:],
            "isolated_output": str(iso_out) if iso_out.exists() else None,
            "explore_output": str(ws / "output_sample.json"),
            "rerun_dir": str(rerun),
            # Output-location signal: non-empty = output_sample.json exists ONLY
            # below a subdirectory. ADVISORY — the agent weighs it for runs_clean
            # (NOT mechanically rejected; the floor only fires on a crash).
            "stray_output_samples": _stray_output_samples(rerun),
            **_list_produced(rerun),
        }
    except subprocess.TimeoutExpired:
        _persist_run_result(rerun, exit_code=None, timed_out=True, traceback=False)
        return {
            "ran": True,
            "exit_code": None,
            "timed_out": True,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
            "rerun_dir": str(rerun),
            **_list_produced(rerun),  # partial products still tell the story
        }


def run_agent_python(
    site_id: str, run_id: str, code: str, *, timeout_s: int = 120, python_exe: str | None = None
) -> dict:
    """Run the validator agent's OWN Python in an isolated scratch it fully owns.

    Isolation (the agreed three-zone model):
      - WORK (full read-write): validation/<run_id>/work/ is the cwd; files
        persist across calls, so the agent can build helper modules and outputs.
      - EXPLORE artifacts (read-only): staged as COPIES into work/inputs_ro/
        (output_sample.json / selectors.yaml / goal.md / manifests). The agent
        reads them there; editing a copy can never touch the real artifact.
      - ADJUDICATION files (zone C — neither read-only nor the agent's):
        scorecard.yaml, rerun/, _run_result.json live in the PARENT
        validation/<run_id>/, are NOT staged into work/, and are NOT in cwd, so
        a relative-path script cannot reach them. Verdicts must still go through
        update_scorecard. (Same trust level as run_workflow_isolated — an
        absolute-path escape is possible; an OS jail is a later hardening.)
    scrubbed_env(): the script never receives the validator's secrets. Returns
    run facts + the work/inputs_ro paths so the agent knows where to read."""
    ws = _workspace(site_id)
    work = ws / "validation" / run_id / "work"
    ro = work / "inputs_ro"
    ro.mkdir(parents=True, exist_ok=True)
    # Refresh read-only copies of the artifacts the agent commonly verifies.
    for src in (
        ws / "output_sample.json",
        ws / "selectors.yaml",
        ws / "api_manifest.yaml",
        ws / "download_manifest.yaml",
        PROJECT_ROOT / "inputs" / site_id / "goal.md",
    ):
        if src.exists():
            shutil.copy2(src, ro / src.name)
    # Also stage the isolated rerun's PRODUCED files (downloaded files /
    # output_sample.json / media) read-only under inputs_ro/rerun/, so the agent
    # can re-parse / sniff / hash the bytes the workflow ACTUALLY produced — the
    # one piece the deterministic checks alone can't deeply re-examine. COPIES
    # only: the canonical rerun/ stays zone-C off-limits, so the agent still
    # cannot forge what the deterministic checks + safety floor read. Staged
    # inputs (workflow.py / auth_state.json / …) and control files (_*) are
    # excluded by _list_produced; cap follows _LIST_CAP. mtime guard avoids
    # re-copying large media on every call (and refreshes after a new rerun).
    rerun = ws / "validation" / run_id / "rerun"
    if rerun.exists():
        for item in _list_produced(rerun)["produced_files"]:
            src = rerun / item["path"]
            dst = ro / "rerun" / item["path"]
            try:
                if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError:
                pass

    # Numbered entry script so successive run_python calls don't clobber.
    n = len(list(work.glob("_agent_*.py"))) + 1
    script = work / f"_agent_{n}.py"
    script.write_text(code, encoding="utf-8")

    py = python_exe or sys.executable
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [py, script.name],
            cwd=work,  # ← the agent's full-rights scratch; zone C is the parent dir
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            env=scrubbed_env(),
        )
        return {
            "ran": True,
            "exit_code": r.returncode,
            "timed_out": False,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
            "stdout_tail": (r.stdout or "")[-4000:],
            "stderr_tail": (r.stderr or "")[-2000:],
            "work_dir": str(work),
            "inputs_ro": str(ro),
            "script": script.name,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ran": True,
            "exit_code": None,
            "timed_out": True,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
            "work_dir": str(work),
        }


# ── dimension: self_consistency (field-name part) ───────────────────


def _unwrap(raw):
    if isinstance(raw, dict):
        for k in ("rows", "records", "data", "items", "results"):
            if isinstance(raw.get(k), list):
                return raw[k]
        return None
    return raw


def _is_empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {} or (isinstance(v, str) and not v.strip())


def compare_field_names(site_id: str, output_path: str | None = None) -> dict:
    """Compare DECLARED field names (selectors.yaml, workflow_field-overridden)
    vs ACTUAL field names (output_sample.json record keys UNION — not [0]).

    Does NOT static-parse workflow.py (structure varies → no general
    extraction; product = truth). Caller passes `output_path` to point at
    the isolated rerun output; defaults to explore's output_sample.json.

    Note: this is the SELF-CONSISTENCY check (declared == produced). It is
    NOT a completeness check — the field set is explore's decision and has
    no independent basis (see memory/reference_verifier_basis_independence)."""
    from mcp_server.schemas import APIManifestFile, SelectorsFile

    ws = _workspace(site_id)
    declared, declared_map = set(), {}
    if (ws / "api_manifest.yaml").exists():
        # api workflows declare fields in the manifest (no selectors.yaml).
        mf = APIManifestFile.load(ws / "api_manifest.yaml")
        for f in mf.fields:
            declared.add(f.name)
            declared_map[f.name] = f.name
    else:
        sf = SelectorsFile.load(ws / "selectors.yaml")  # Pydantic — fails loud on bad shape
        for canonical, entry in sf.selectors.fields.items():
            eff = entry.workflow_field or canonical
            declared.add(eff)
            declared_map[canonical] = eff

    out_path = Path(output_path) if output_path else (ws / "output_sample.json")
    if not out_path.exists():
        return {"error": "output not found", "output_path": str(out_path)}
    raw = json.loads(out_path.read_text(encoding="utf-8-sig"))
    records = _unwrap(raw)
    if not isinstance(records, list) or not records:
        return {"error": "output has no record list", "output_path": str(out_path)}
    dicts = [r for r in records if isinstance(r, dict)]
    actual = set().union(*(r.keys() for r in dicts)) if dicts else set()

    per_field = {}
    for k in sorted(actual):
        present = sum(1 for r in dicts if k in r)
        nonnull = sum(1 for r in dicts if k in r and not _is_empty(r[k]))
        seen, sample = set(), []
        for r in dicts:
            if k not in r:
                continue
            v = r[k]
            disp = f"<{type(v).__name__}>" if isinstance(v, (list, dict)) else _clip(v)
            sig = repr(disp)[:60]
            if sig not in seen:
                seen.add(sig)
                if len(sample) < 5:
                    sample.append(disp)
        per_field[k] = {
            "present_in": present,
            "nonnull_in": nonnull,
            "distinct_capped": len(seen),
            "distinct_sample": sample,
        }

    return {
        "record_count": len(dicts),
        "declared": sorted(declared),
        "declared_map": declared_map,
        "actual_union": sorted(actual),
        "never_appeared": sorted(declared - actual),  # LLM judges: bug vs sparse
        "undeclared": sorted(actual - declared),
        "schema_consistent": len({frozenset(r.keys()) for r in dicts}) <= 1,
        "per_field": per_field,
        "output_path": str(out_path),
    }


# ── dimension: reproducibility (check 4) ────────────────────────────


def _load_records(path: Path):
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    recs = _unwrap(raw)
    if not isinstance(recs, list):
        return None
    return [r for r in recs if isinstance(r, dict)]


def _within_tolerance(a, b, *, abs_tol: float = 5.0, pct_tol: float = 0.05) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        if isinstance(a, str) and isinstance(b, str):
            # TOLERANT strings (weak-schema page-sized content): live-page
            # drift keeps roughly the same length and an identical opening;
            # a changed length OR opening means different content, not drift.
            # Strings <= _STR_PREFIX degrade to exact equality (prefix == whole).
            if a == b:
                return True
            len_ok = abs(len(a) - len(b)) <= _STR_LEN_TOL * max(len(a), len(b), 1)
            return len_ok and a[:_STR_PREFIX] == b[:_STR_PREFIX]
        return a == b  # non-numeric, non-string pair → exact
    d = abs(fa - fb)
    return d <= abs_tol or d / max(abs(fa), 1.0) <= pct_tol


def compare_output(
    site_id: str, rerun_output_path: str, *, identifier_field: str = "post_url"
) -> dict:
    """Compare explore's output_sample.json vs the ISOLATED rerun output:
    record count, field keys, per-field values. Tolerance per field comes
    from selectors.yaml `field_stability` (STRICT/TOLERANT/SKIP); fields
    with no class land in `unclassified` (LLM decides — not auto-fail).

    Same workflow.py run twice should match modulo real-time drift:
    STRICT mismatch = reproducibility failure; TOLERANT drift = a note."""
    from mcp_server.schemas import SelectorsFile

    ws = _workspace(site_id)
    explore = _load_records(ws / "output_sample.json")
    rerun = _load_records(Path(rerun_output_path))
    if explore is None:
        return {"error": "explore output_sample.json missing/invalid"}
    if rerun is None:
        return {"error": "rerun output missing/invalid", "rerun_output_path": rerun_output_path}

    classes: dict[str, str] = {}
    try:
        sf = SelectorsFile.load(ws / "selectors.yaml")
        for name, e in sf.field_stability.fields.items():
            classes[name] = e.suggested_class
    except Exception:  # noqa: BLE001 — no/invalid selectors → everything unclassified
        pass
    if not classes:
        # api workflows: stability classes live in api_manifest.yaml fields[].
        try:
            from mcp_server.schemas import APIManifestFile

            mf = APIManifestFile.load(ws / "api_manifest.yaml")
            classes = {f.name: f.stability for f in mf.fields if f.stability}
        except Exception:  # noqa: BLE001 — no/invalid manifest → unclassified
            pass

    ek = set().union(*(r.keys() for r in explore)) if explore else set()
    rk = set().union(*(r.keys() for r in rerun)) if rerun else set()

    # Weak-schema fallback: the default identifier (post_url) is a tabular-era
    # convention; weak-schema records are only guaranteed `source_url`. When
    # the requested identifier is carried by NEITHER side's records but
    # source_url is carried by both, match on source_url instead (reported
    # via identifier_fallback so the agent knows what was actually compared).
    requested_identifier = identifier_field

    def _usable(field: str) -> bool:
        return any(r.get(field) for r in explore) and any(r.get(field) for r in rerun)

    identifier_fallback = False
    if not _usable(identifier_field) and identifier_field != "source_url" and _usable("source_url"):
        identifier_field, identifier_fallback = "source_url", True

    rerun_by_id = {r.get(identifier_field): r for r in rerun if r.get(identifier_field)}

    strict_mm, tol_warn, unclassified = [], [], set()
    matched = 0
    for er in explore:
        rid = er.get(identifier_field)
        rr = rerun_by_id.get(rid)
        if rid is None or rr is None:
            continue
        matched += 1
        for f, ev in er.items():
            cls = classes.get(f)
            if cls == "SKIP":
                continue
            rv = rr.get(f)
            if cls == "STRICT":
                if ev != rv:
                    strict_mm.append(
                        {"id": rid, "field": f, "explore": _clip(ev), "rerun": _clip(rv)}
                    )
            elif cls == "TOLERANT":
                if not _within_tolerance(ev, rv):
                    tol_warn.append(
                        {"id": rid, "field": f, "explore": _clip(ev), "rerun": _clip(rv)}
                    )
            else:
                unclassified.add(f)

    return {
        "explore_count": len(explore),
        "rerun_count": len(rerun),
        "count_delta": len(rerun) - len(explore),
        "field_keys_match": ek == rk,
        "explore_only_keys": sorted(ek - rk),
        "rerun_only_keys": sorted(rk - ek),
        "matched_by_id": matched,
        "identifier_field": identifier_field,
        "identifier_requested": requested_identifier,
        "identifier_fallback": identifier_fallback,
        "strict_mismatch_count": len(strict_mm),
        "strict_mismatches": strict_mm[:20],
        "tolerant_warning_count": len(tol_warn),
        "tolerant_warnings": tol_warn[:20],
        "unclassified_fields": sorted(unclassified),
    }


# ── weak-schema: file_ref targets (feeds self_consistency) ──────────


def verify_file_refs(site_id: str, output_path: str | None = None) -> dict:
    """Weak-schema deliverables: every record's `file_ref` must point at an
    existing, NON-EMPTY file. Existence + size only — the contract says the
    validator does not re-extract the bytes (SKILL "Weak-schema data").

    Resolution base = the output file's directory: the contract writes
    file_ref relative to the producing run's cwd, which is exactly where
    output_sample.json lands (explore → the workspace root; isolated rerun →
    validation/<run_id>/rerun/ — pass output_path=<isolated_output>).
    Absolute paths and paths escaping the base directory are CONTRACT
    VIOLATIONS, reported without a lookup.

    A record with no `file_ref` key is fine (inline `content` carrier);
    applicable=false ⇔ NO record carries one — nothing to check, not a
    failure. A missing/empty target is self_consistency FAIL evidence
    (the output claims a file that isn't there)."""
    ws = _workspace(site_id)
    out = Path(output_path) if output_path else (ws / "output_sample.json")
    records = _load_records(out)
    if records is None:
        return {"error": "output missing or not a record list", "output_path": str(out)}
    base = out.parent.resolve()

    missing: list[dict] = []
    empty: list[dict] = []
    violations: list[dict] = []
    refs_total = ok = 0
    for i, r in enumerate(records):
        if "file_ref" not in r:
            continue
        refs_total += 1
        ref = r["file_ref"]
        item: dict = {"record_index": i, "file_ref": ref if isinstance(ref, str) else repr(ref)}
        if not isinstance(ref, str) or not ref.strip():
            item["issue"] = "file_ref is not a non-empty string"
            violations.append(item)
            continue
        p = Path(ref)
        if p.is_absolute():
            item["issue"] = "absolute path (contract: relative to the producing run's cwd)"
            violations.append(item)
            continue
        resolved = (base / p).resolve()
        if base != resolved and base not in resolved.parents:
            item["issue"] = "path escapes the output directory"
            violations.append(item)
            continue
        if not resolved.is_file():
            item["exists"] = False
            missing.append(item)
            continue
        size = resolved.stat().st_size
        item.update({"exists": True, "bytes": size})
        if size == 0:
            empty.append(item)
        else:
            ok += 1

    if refs_total == 0:
        return {
            "applicable": False,
            "reason": "no record carries file_ref",
            "records": len(records),
            "output_path": str(out),
        }
    return {
        "applicable": True,
        "output_path": str(out),
        "base_dir": str(base),
        "records": len(records),
        "refs_total": refs_total,
        "refs_ok": ok,
        "all_ok": ok == refs_total,
        "missing_total": len(missing),
        "empty_total": len(empty),
        "violations_total": len(violations),
        "missing": missing[:_LIST_CAP],
        "empty": empty[:_LIST_CAP],
        "violations": violations[:_LIST_CAP],
    }


__all__ = [
    "check_workflow_syntax",
    "run_workflow_isolated",
    "compare_field_names",
    "compare_output",
    "verify_file_refs",
]
