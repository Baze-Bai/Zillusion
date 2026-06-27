"""SDK MCP tool surface for the validator agent.

Wraps the deterministic checks (validator_checks), the playwright checks
(validator_browser), and the scorecard/report/feedback writers into an
in-process SDK MCP server via `create_sdk_mcp_server`. This is the agent's
ENTIRE capability surface — there is no general Bash/Write/Edit, so the
validator physically cannot modify an explore artifact (isolation by tool
surface, per memory/reference_validator_6tool_architecture).

All writes target `workspaces/<site>/validation/<run_id>/` only:
  - scorecard.yaml (gate-computed verdict)
  - report.md      (narrative)
  - feedback.yaml  (hypothesis feedback for the next /explore)
  - samples/, rerun/  (written by inspect/run tools)
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from runtime.validator_checks import (
    PROJECT_ROOT,
    check_workflow_syntax,
    compare_field_names,
    compare_output,
    run_agent_python,
    run_workflow_isolated,
    verify_file_refs,
)
from runtime.validator_api import check_no_secret_leak, probe_api_endpoint
from runtime.validator_browser import (
    browser_content,
    browser_evaluate,
    browser_goto,
    inspect_record_html,
)
from runtime.validator_download import verify_downloaded_files
from mcp_server.schemas import ScorecardFile, new_scorecard


# ── write backends (validation/<run_id>/ only) ──────────────────────


def _val_dir(site_id: str, run_id: str) -> Path:
    return PROJECT_ROOT / "workspaces" / site_id / "validation" / run_id


def _detect_workflow_type(site_id: str) -> str:
    """Workspace-artifact truth for the workflow type (mirrors
    runtime.validate._detect_workflow_type; duplicated here because validate
    imports this module — importing back would cycle)."""
    ws = PROJECT_ROOT / "workspaces" / site_id
    if (ws / "api_manifest.yaml").exists():
        return "api"
    if (ws / "download_manifest.yaml").exists():
        return "download"
    if (PROJECT_ROOT / "inputs" / site_id / "api_spec.json").exists():
        return "api"
    return "extraction"


def _floor_violation(site_id: str, run_id: str, dim: str) -> str | None:
    """The thin SAFETY FLOOR — three cheap, certain, dangerous-to-wave
    conditions the validator agent may NEVER record as pass/n-a. Each is
    re-derived from DISK (not a relayed tool result) so the agent cannot spoof
    it. EVERYTHING outside this floor is the agent's judgment (informed by the
    check tools, decided by the LLM). Returns a reason string when the dim is
    floored in the current on-disk state, else None.

    Floor = {syntax parse-fail, runs_clean CRASH, secrets shipped-leak}. Note
    what is DELIBERATELY NOT floored: a rerun TIMEOUT (a correct-but-slow crawl
    on a complex output is not a defect), and the output-location contract
    (stray output_sample.json) — both are advisory now; the agent weighs the
    evidence run_workflow_isolated surfaces and decides."""
    if dim == "syntax":
        res = check_workflow_syntax(site_id)
        if not res.get("ok"):
            errs = res.get("errors") or []
            return f"workflow.py does not parse (syntax) — {errs[:2]}; syntax must be fail."
    elif dim == "runs_clean":
        rr = _val_dir(site_id, run_id) / "rerun" / "_run_result.json"
        if rr.exists():
            try:
                data = json.loads(rr.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if data.get("crashed"):
                return (
                    f"the isolated rerun CRASHED (exit_code={data.get('exit_code')}, "
                    f"traceback={data.get('traceback')}) — runs_clean must be fail. "
                    f"(A timeout or a misplaced output file is NOT a crash — judge those yourself.)"
                )
    elif dim == "secrets_safe":
        try:
            res = check_no_secret_leak(site_id, run_id)
        except Exception:  # noqa: BLE001 — a scan failure must not floor; let the agent judge
            return None
        if not res.get("na") and not res.get("ok"):
            files = [leak.get("file") for leak in (res.get("leaks") or [])][:3]
            return (
                f"a credential literal leaks into shipped artifact(s) {files} — "
                f"secrets_safe must be fail."
            )
    return None


def init_scorecard(site_id: str, run_id: str, workflow_type: str = "extraction") -> dict:
    # The dimension catalog FOLLOWS the workspace artifacts, not the caller:
    # accepting a mismatched type would swap the gating dimension set (e.g.
    # extraction → download drops the runs_clean output-contract gate).
    detected = _detect_workflow_type(site_id)
    sc = new_scorecard(run_id, site_id, detected)
    d = _val_dir(site_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    sc.save(d / "scorecard.yaml")
    res = {
        "created": f"validation/{run_id}/scorecard.yaml",
        "workflow_type": detected,
        "dimensions": sorted(sc.dimensions),
        "overall": sc.overall,
    }
    if workflow_type != detected:
        res["note"] = (
            f"requested workflow_type {workflow_type!r} ignored — workspace artifacts "
            f"declare {detected!r}; the scorecard uses the declared type."
        )
    return res


def update_scorecard(
    site_id: str, run_id: str, dim: str, status: str, basis: str = "", evidence: str = ""
) -> dict:
    p = _val_dir(site_id, run_id) / "scorecard.yaml"
    sc = ScorecardFile.load(p)
    # SAFETY FLOOR: three disk-grounded, dangerous-to-wave conditions the agent
    # may never record as pass/n-a (syntax parse-fail / runs_clean crash /
    # secrets shipped-leak). Everything else is the agent's judgment — the tool
    # results inform, the LLM decides. (The old output-location HARD gate is now
    # advisory: run_workflow_isolated still surfaces stray_output_samples; the
    # agent weighs it.)
    if status in ("pass", "n/a"):
        violation = _floor_violation(site_id, run_id, dim)
        if violation is not None:
            return {
                "rejected": True,
                "dim": dim,
                "attempted_status": status,
                "reason": violation,
                "required": f"record {dim} as fail",
            }
    sc.set_dimension(dim, status, basis=basis or None, evidence=evidence or None)
    sc.save(p)
    return {"dim": dim, "status": status, "overall": sc.overall}


def read_scorecard(site_id: str, run_id: str) -> dict:
    sc = ScorecardFile.load(_val_dir(site_id, run_id) / "scorecard.yaml")
    return {
        "overall": sc.overall,
        "dimensions": {
            n: {"status": d.status, "gating": d.gating, "evidence": d.evidence}
            for n, d in sc.dimensions.items()
        },
    }


def write_report(site_id: str, run_id: str, section: str) -> dict:
    d = _val_dir(site_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    rp = d / "report.md"
    prev = rp.read_text(encoding="utf-8") if rp.exists() else "# Validation report\n\n"
    rp.write_text(prev + section.rstrip() + "\n\n", encoding="utf-8")
    return {"written": f"validation/{run_id}/report.md"}


def append_feedback(
    site_id: str,
    run_id: str,
    claim: str,
    basis: str = "",
    priority: str = "medium",
    notes: str = "",
) -> dict:
    d = _val_dir(site_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    fp = d / "feedback.yaml"
    data = yaml.safe_load(fp.read_text(encoding="utf-8")) if fp.exists() else None
    items = data if isinstance(data, list) else []
    items.append({"claim": claim, "basis": basis, "priority": priority, "notes": notes})
    fp.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"appended": claim, "total": len(items)}


# ── @tool wrappers (MCP result format) ──────────────────────────────


def _ok(d: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(d, ensure_ascii=False, default=str)}]}


def _safe(fn):
    """Wrap a tool handler so any exception becomes a recoverable error result.

    The SDK invokes handlers without a try/except, so a raised exception
    (e.g. ScorecardFile.load before init_scorecard, a missing selectors.yaml,
    or a playwright nav timeout) escapes as a session error and aborts the
    whole validation run. With this wrapper the validator instead sees the
    error text and can record the affected dimension as not_verified.
    """

    @functools.wraps(fn)
    async def wrapper(args):
        try:
            return await fn(args)
        except Exception as exc:  # noqa: BLE001 — deliberately broad; surfaced to the agent
            return _ok({"error": f"{type(exc).__name__}: {exc}"})

    return wrapper


_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


def _schema(props: dict, required: list[str]) -> dict:
    """Build an explicit JSON Schema so OPTIONAL params aren't force-marked required.

    The SDK's flat ``{name: type}`` form marks EVERY key required
    (claude_agent_sdk __init__.py: ``"required": list(properties.keys())``),
    which misinforms the model about optional args and makes ``selector=None``
    style overview modes unreachable. Tools with optional params pass full
    schema via this helper instead.

    A param typed ``object``/``array`` (dict/list) maps to that JSON type; an
    unmapped type (e.g. ``typing.Any``, for a free-form cursor) is left
    unconstrained rather than raising.
    """

    def _prop(t) -> dict:
        jt = _JSON_TYPES.get(t)
        return {"type": jt} if jt else {}

    return {
        "type": "object",
        "properties": {k: _prop(t) for k, t in props.items()},
        "required": required,
    }


@tool("check_workflow_syntax", "ast.parse workflow.py. Dimension: syntax.", {"site_id": str})
@_safe
async def _t_syntax(args):
    return _ok(check_workflow_syntax(args["site_id"]))


@tool(
    "run_workflow_isolated",
    "Run workflow.py in an isolated validation/<run_id>/rerun/ workdir — NEVER "
    "overwrites explore's output_sample.json. Returns produced_files: everything "
    "the workflow wrote (media/, weak-schema file_ref targets, …), so a deliverable "
    "that is NOT output_sample.json is still visible. A non-empty stray_output_samples "
    "surfaces a likely output-location issue (output_sample.json only below a subdir) — the "
    "agent WEIGHS it for runs_clean (advisory, NOT mechanically rejected). Dimension: runs_clean.",
    {"site_id": str, "run_id": str},
)
@_safe
async def _t_run(args):
    return _ok(run_workflow_isolated(args["site_id"], args["run_id"]))


@tool(
    "run_python",
    "Write+run your OWN Python in an ISOLATED scratch you fully own "
    "(validation/<run_id>/work/, the cwd; files persist across calls so you can "
    "build helpers/outputs). Staged READ-ONLY in work/inputs_ro/: explore artifacts "
    "(output_sample.json/selectors.yaml/goal.md/manifests) AND, after run_workflow_isolated, "
    "the rerun's PRODUCED files under inputs_ro/rerun/ (downloaded files / output_sample.json / "
    "media) — so you can re-parse/sniff/hash the bytes the workflow ACTUALLY produced. Read them "
    "there; you cannot modify the real artifacts. Use it to flexibly judge ANY dimension: fetch "
    "live data (httpx), paginate an API, parse, hash, normalize a volatile field, diff vs the "
    "sample. scrubbed_env (no secrets). You still CANNOT reach scorecard.yaml / the canonical "
    "rerun/ dir / the floor signals from here — record verdicts ONLY via update_scorecard. "
    "timeout_s optional (default 120).",
    _schema(
        {"site_id": str, "run_id": str, "code": str, "timeout_s": int},
        ["site_id", "run_id", "code"],
    ),
)
@_safe
async def _t_run_python(args):
    return _ok(
        run_agent_python(
            args["site_id"],
            args["run_id"],
            args["code"],
            timeout_s=args.get("timeout_s") or 120,
        )
    )


@tool(
    "verify_file_refs",
    "Weak-schema records: check every record's file_ref target exists + is non-empty "
    "(existence/size only — bytes are not re-extracted). Refs resolve against the output "
    "file's directory: default = explore's output_sample.json (workspace root); pass "
    "output_path=<isolated_output> to check the rerun's refs. applicable=false → no record "
    "carries file_ref (nothing to check). Missing/empty target or an absolute/escaping "
    "path is self_consistency FAIL evidence. Dimension: self_consistency.",
    _schema({"site_id": str, "output_path": str}, ["site_id"]),
)
@_safe
async def _t_file_refs(args):
    return _ok(verify_file_refs(args["site_id"], args.get("output_path") or None))


@tool(
    "compare_field_names",
    "Declared (selectors.yaml) vs produced (output keys union) field names. "
    "Pass output_path for the isolated rerun output (optional). Dimension: self_consistency.",
    _schema({"site_id": str, "output_path": str}, ["site_id"]),
)
@_safe
async def _t_fields(args):
    return _ok(compare_field_names(args["site_id"], args.get("output_path") or None))


@tool(
    "compare_output",
    "Explore output vs isolated rerun output: count/keys/values with field_stability "
    "tolerance. identifier_field optional (default post_url). Dimension: reproducibility.",
    _schema(
        {"site_id": str, "rerun_output_path": str, "identifier_field": str},
        ["site_id", "rerun_output_path"],
    ),
)
@_safe
async def _t_output(args):
    return _ok(
        compare_output(
            args["site_id"],
            args["rerun_output_path"],
            identifier_field=args.get("identifier_field") or "post_url",
        )
    )


@tool(
    "browser_goto",
    "Navigate the validator's browser to a URL (auth + anti-bot applied; page state "
    "persists across browser_* calls). scroll=true scrolls to the bottom for lazy / "
    "infinite-scroll pages. Use this to drive the live page yourself when judging "
    "resample_match / field_semantics — go to a detail page, a listing, or an API URL.",
    _schema({"url": str, "scroll": bool, "timeout_ms": int}, ["url"]),
)
@_safe
async def _t_browser_goto(args):
    return _ok(
        await browser_goto(
            args["url"], scroll=bool(args.get("scroll")), timeout_ms=args.get("timeout_ms") or 60000
        )
    )


@tool(
    "browser_content",
    "Return the current page's HTML (optionally goto url first). Pass a CSS selector to "
    "scope to a sub-tree; no selector returns the whole page (capped; chars_total reports "
    "the true size). Read the live DOM to judge a record / selector.",
    _schema({"url": str, "selector": str, "timeout_ms": int}, []),
)
@_safe
async def _t_browser_content(args):
    return _ok(
        await browser_content(
            url=args.get("url") or None,
            selector=args.get("selector") or None,
            timeout_ms=args.get("timeout_ms") or 60000,
        )
    )


@tool(
    "browser_evaluate",
    "Run YOUR OWN JS on the page (optionally goto url first) and return its result. The JS "
    "runs in the browser SANDBOX: it can fetch() any URL with the page's cookies/origin "
    "(re-call the site's paginated API to get ALL records), read the DOM, scroll, normalise "
    "volatile values — but cannot touch host files. This is how you verify resample_match "
    "flexibly (deep pagination / detail pages / CSR). Return a small JSON-serialisable value.",
    _schema({"js": str, "url": str, "timeout_ms": int}, ["js"]),
)
@_safe
async def _t_browser_evaluate(args):
    return _ok(
        await browser_evaluate(
            args["js"], url=args.get("url") or None, timeout_ms=args.get("timeout_ms") or 60000
        )
    )


@tool(
    "inspect_record_html",
    "Fetch one record's page structure to judge if a selector maps to the RIGHT field. "
    "Cleaned record saved to samples/; matched element RAW inlined. record_index + selector "
    "optional (omit selector for a lighter page overview). Dimension: field_semantics.",
    _schema(
        {"site_id": str, "run_id": str, "source_url": str, "record_index": int, "selector": str},
        ["site_id", "run_id", "source_url"],
    ),
)
@_safe
async def _t_inspect(args):
    return _ok(
        await inspect_record_html(
            args["site_id"],
            args["run_id"],
            args["source_url"],
            record_index=args.get("record_index") or 0,
            selector=args.get("selector") or None,
        )
    )


@tool(
    "init_scorecard",
    "Create a fresh scorecard at run start. The dimension catalog (extraction 8 record dims / "
    "download file dims / api live-endpoint + secrets dims) follows the WORKSPACE's declared "
    "workflow type — a passed workflow_type that disagrees is ignored (reported in the result).",
    _schema({"site_id": str, "run_id": str, "workflow_type": str}, ["site_id", "run_id"]),
)
@_safe
async def _t_init(args):
    return _ok(
        init_scorecard(args["site_id"], args["run_id"], args.get("workflow_type") or "extraction")
    )


@tool(
    "update_scorecard",
    "Set one dimension's status (pass/fail/not_verified/advisory/n/a) + basis + evidence. "
    "basis + evidence optional. Returns the recomputed overall.",
    _schema(
        {"site_id": str, "run_id": str, "dim": str, "status": str, "basis": str, "evidence": str},
        ["site_id", "run_id", "dim", "status"],
    ),
)
@_safe
async def _t_update(args):
    return _ok(
        update_scorecard(
            args["site_id"],
            args["run_id"],
            args["dim"],
            args["status"],
            basis=args.get("basis") or "",
            evidence=args.get("evidence") or "",
        )
    )


@tool(
    "read_scorecard",
    "Read current scorecard state + computed overall.",
    {"site_id": str, "run_id": str},
)
@_safe
async def _t_read(args):
    return _ok(read_scorecard(args["site_id"], args["run_id"]))


@tool(
    "write_report",
    "Append a narrative section to validation/<run_id>/report.md.",
    {"site_id": str, "run_id": str, "section": str},
)
@_safe
async def _t_report(args):
    return _ok(write_report(args["site_id"], args["run_id"], args["section"]))


@tool(
    "append_feedback",
    "Append a hypothesis-feedback item to validation/<run_id>/feedback.yaml (NOT explore's "
    "hypotheses.yaml). The next /explore reads it and appends to its own. basis/priority/notes optional.",
    _schema(
        {"site_id": str, "run_id": str, "claim": str, "basis": str, "priority": str, "notes": str},
        ["site_id", "run_id", "claim"],
    ),
)
@_safe
async def _t_feedback(args):
    return _ok(
        append_feedback(
            args["site_id"],
            args["run_id"],
            args["claim"],
            basis=args.get("basis") or "",
            priority=args.get("priority") or "medium",
            notes=args.get("notes") or "",
        )
    )


@tool(
    "probe_api_endpoint",
    "API workflows: ONE live call to api_manifest.yaml's declared probe_url (auth applied per "
    "the manifest); saves the full body under validation/<run_id>/samples/, returns status / "
    "rate-limit headers / a bounded raw_preview / value_match (which output_sample values "
    "appear in the live body). Low hit ratios can be legitimate transforms — judge from "
    "raw_preview, don't auto-fail. endpoint_index + sample_size optional. Dimension: endpoint_match.",
    _schema(
        {"site_id": str, "run_id": str, "endpoint_index": int, "sample_size": int},
        ["site_id", "run_id"],
    ),
)
@_safe
async def _t_probe_api(args):
    return _ok(
        await probe_api_endpoint(
            args["site_id"],
            args["run_id"],
            endpoint_index=args.get("endpoint_index") or 0,
            sample_size=args.get("sample_size") or 3,
        )
    )


@tool(
    "check_no_secret_leak",
    "API workflows: scan shipped artifacts (workflow.py / helpers.py / api_manifest.yaml / "
    "output_sample.json + the isolated rerun output) for credential literals from "
    "inputs/<site>/credentials.json. na=true → record the dimension as n/a (open API). "
    "Dimension: secrets_safe.",
    {"site_id": str, "run_id": str},
)
@_safe
async def _t_secret_leak(args):
    return _ok(check_no_secret_leak(args["site_id"], args["run_id"]))


@tool(
    "verify_downloaded_files",
    "DOWNLOAD workflows: verify the files declared in download_manifest.yaml against the "
    "isolated rerun output — exists / non_empty / format_valid / parses. Run "
    "run_workflow_isolated first. Maps to dims: file_downloaded, non_empty, format_valid, parses.",
    {"site_id": str, "run_id": str},
)
@_safe
async def _t_verify_download(args):
    return _ok(verify_downloaded_files(args["site_id"], args["run_id"]))


VALIDATOR_TOOLS = [
    _t_syntax,
    _t_run,
    _t_run_python,
    _t_fields,
    _t_output,
    _t_file_refs,
    _t_browser_goto,
    _t_browser_content,
    _t_browser_evaluate,
    _t_inspect,
    _t_verify_download,
    _t_probe_api,
    _t_secret_leak,
    _t_init,
    _t_update,
    _t_read,
    _t_report,
    _t_feedback,
]


def build_validator_server():
    """In-process SDK MCP server exposing the validator's entire tool surface."""
    return create_sdk_mcp_server("validator-tools", version="0.1.0", tools=VALIDATOR_TOOLS)


__all__ = ["build_validator_server", "VALIDATOR_TOOLS"]
