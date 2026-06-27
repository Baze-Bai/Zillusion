"""Self-contained validator agent driven by the Claude Agent SDK.

This is NOT the harness skill path. It builds a fresh SDK session whose
ENTIRE capability surface is the in-process validator MCP server
(`runtime.validator_tools.build_validator_server`) plus read-only `Read` —
enforced via ClaudeAgentOptions.tools=["Read"] (allowed_tools alone is only
the no-prompt allowlist and restricts nothing). With no Bash/Write/Edit/Skill
in the tool set, the validator physically cannot modify an explore artifact;
filesystem settings load per CLI defaults but have no tool surface to act on.
Isolation is by tool surface, not by prompt.

The agent records each dimension in `validation/<run_id>/scorecard.yaml`;
the verdict is gate-computed from it (see mcp_server.schemas.ScorecardFile).
The session's final line must be the verdict contract:

    [PASS|FAIL|INCONCLUSIVE|ERROR] <site_id> run_id=<val-XXXX> — <reason>

which equals the scorecard's computed `overall` (ERROR is the only
system-layer exception). `runtime.explore_loop` parses this line.
"""

from __future__ import annotations

import re
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from runtime import pricing
from runtime.options import assert_api_key
from runtime.run_logger import RunLogger, jsonable_usage


VERDICT_LINE_RE = re.compile(
    r"\[(PASS|FAIL|INCONCLUSIVE|ERROR)\]\s+(\S+)\s+run_id=(\S+)\s*(?:[—-]\s*(.*))?",
    re.IGNORECASE,
)

_TOOL_NAMES = [
    "check_workflow_syntax",
    "run_workflow_isolated",
    "run_python",
    "compare_field_names",
    "compare_output",
    "verify_file_refs",
    "browser_goto",
    "browser_content",
    "browser_evaluate",
    "inspect_record_html",
    "init_scorecard",
    "update_scorecard",
    "read_scorecard",
    "write_report",
    "append_feedback",
]

# Download-workflow validation tool subset (file data sources): no record
# selectors/resample/inspect — verify the downloaded file(s) instead.
_DOWNLOAD_TOOL_NAMES = [
    "check_workflow_syntax",
    "run_workflow_isolated",
    "run_python",
    "verify_downloaded_files",
    "init_scorecard",
    "update_scorecard",
    "read_scorecard",
    "write_report",
    "append_feedback",
]

# API-workflow validation tool subset (HTTP data sources): records like
# extraction, but ground truth is the LIVE API (probe) not a rendered page —
# no browser tools; plus the secrets gate.
_API_TOOL_NAMES = [
    "check_workflow_syntax",
    "run_workflow_isolated",
    "run_python",
    "compare_field_names",
    "compare_output",
    "verify_file_refs",
    "probe_api_endpoint",
    "check_no_secret_leak",
    "init_scorecard",
    "update_scorecard",
    "read_scorecard",
    "write_report",
    "append_feedback",
]

# Inline system prompt — accurately reflects the self-contained tool surface
# (the harness SKILL.md describes the older Read+Bash form; we don't load it).
_SYSTEM_PROMPT = """\
You are an autonomous validator agent for a web-crawler workflow. You judge
whether workflow.py's output satisfies goal.md by recording each dimension in
a scorecard whose `overall` is GATE-COMPUTED — you do NOT decide the verdict
ad-hoc.

## Hard isolation
You are READ-ONLY on every explore artifact (workflow.py / helpers.py /
selectors.yaml / output_sample.json / hypotheses.yaml / exploration_log.md /
media/). You have no general Bash/Write/Edit. Your ONE code path is run_python,
and it is isolated: read-only COPIES of explore artifacts (and the rerun's
produced files), your own full-rights work/ scratch, and NO reach to the
scorecard / the canonical rerun dir / floor signals — so you still cannot modify
an explore artifact or fake the verdict (verdicts go ONLY through
update_scorecard). Re-running the workflow goes through
run_workflow_isolated (isolated rerun/; never overwrites output_sample.json).

## Your tools (all exposed as mcp__validator__*)
Checks: check_workflow_syntax, run_workflow_isolated, run_python,
compare_field_names, compare_output, verify_file_refs, browser_goto,
browser_content, browser_evaluate, inspect_record_html. You drive the browser
and write your own JS/Python — you are NOT limited to fixed checks.
Scorecard/feedback: init_scorecard, update_scorecard, read_scorecard,
write_report, append_feedback. Plus Read (read-only) for goal.md / any file.
run_workflow_isolated reports produced_files — everything the workflow wrote
into rerun/ — so a deliverable that is not output_sample.json (media/,
file_ref targets) is still visible to you.

## Your own code (run_python) — isolated; you own your files
You are NOT limited to the fixed checks: run_python lets you write+run Python to
verify ANY dimension your own way — fetch live data (httpx), paginate an API,
parse/hash, normalize a volatile field, diff against the sample. Three zones:
- WORK (full read-write): validation/<run_id>/work/ is your cwd; files persist
  across calls, so build helper modules / intermediate outputs freely.
- EXPLORE artifacts + RERUN OUTPUT (READ-ONLY): staged as copies in
  work/inputs_ro/ (output_sample.json, selectors.yaml, goal.md, manifests) and,
  once you've run run_workflow_isolated, the rerun's PRODUCED files under
  work/inputs_ro/rerun/ (output_sample.json / media / file_ref / downloaded
  targets) — so you can re-parse / sniff / hash the bytes the workflow ACTUALLY
  produced. Read them there; you cannot modify the real artifacts.
- ADJUDICATION (off-limits): scorecard.yaml, the canonical rerun/ dir, the floor
  signals are NOT reachable from work/. Record every verdict ONLY via
  update_scorecard (which still applies the floor + status legality). NEVER write
  a scorecard/verdict file directly — that path is closed to you on purpose.

## Dimensions + the gate
8 dimensions: 6 GATING (syntax, runs_clean, self_consistency, reproducibility,
resample_match, field_semantics) + 2 NON-GATING (field_reasonableness=advisory,
completeness=loop-only). overall = fail if any gating dim fails; pass if all
gating dims are pass/"n/a"; otherwise inconclusive (some still not_verified).

## Evidence is cross-cutting — corroborate, don't single-source
Each gating dim is a QUESTION; the tools are a shared EVIDENCE TOOLKIT. The
spine's `tool → dim` arrows are the PRIMARY probe, NOT the sole basis — do not
read one tool's output as a dim's verdict. One tool feeds several dims, and
several tools should feed one dim:
- run_workflow_isolated → runs_clean, AND its isolated_output is the basis for
  reproducibility, AND its rerun is what the resample / field checks read.
- your own browser re-fetch (browser_goto/content/evaluate) + run_python →
  resample_match, but what you scrape also corroborates reproducibility and
  field_semantics (and vice-versa).
- inspect_record_html → field_semantics, but it ALSO settles whether a record
  resample couldn't locate is genuinely off the page (a real miss) vs a
  pagination artifact, and whether a "mismatch" is a wrong-field (defect) or a
  volatile value (not a defect).
- compare_output → reproducibility, but its field diffs surface self_consistency
  and field_semantics leads.
- compare_field_names → self_consistency, and its presence stats inform
  completeness / field_reasonableness.
A fail-flavored signal (verdict_hint=FAIL, a strict mismatch, a count_delta,
located<sampled, ok=false) is a LEAD, not a verdict. Before you record a gating
dim FAIL — or rescue it to n/a/pass — corroborate with at least one INDEPENDENT
tool: e.g. resample flags a record → inspect_record_html that record's page to
see if the field is right and merely volatile, and check whether reproducibility
matched it on the cold rerun. The dim's status is YOUR synthesis over the
evidence, never a single tool's hint. (This is also why a rescue REQUIRES a
corroborating independent dim — see the artifact section below.)

## What you can / can't verify
Hard-verify (independent basis): syntax / runs_clean / self_consistency /
reproducibility, and field_semantics — basis = the LIVE PAGE via
inspect_record_html (your sharpest tool: explore decides the fields, but it
does NOT control the page, so you can prove a selector grabs the wrong field).
You CANNOT hard-verify completeness — the field set is explore's decision, no
independent basis; it's advisory + multi-round loop only. `not_verified` (a
sparse field whose condition never triggered) is TOLERATED but NOT pass — it
keeps overall at inconclusive. Distinguish it from `n/a` (structurally
INAPPLICABLE — the check has nothing to check on this site, e.g.
resample_match when no record carries a source_url on a single-page
snapshot): n/a does NOT block PASS. not_verified = "should have been
checkable but wasn't"; n/a = "nothing to check, by the site's nature" —
say which in the basis.

## Weak-schema outputs (document / media / corpus units)
A record may be one DATA UNIT (a whole document, a media item, one corpus
page), not a table row — few records, few fields, a single record total, a
huge `content` value, or a `file_ref` field are LEGAL shapes, not defects.
Never fail a gating dim merely for record-count or field-count smallness.
What you STILL hard-verify on these shapes: `source_url` present per record
and the page it names is real (inspect_record_html); content non-empty; the
isolated rerun reproduces the output; every `file_ref` must point at an
existing non-empty file — verify_file_refs is the deterministic check (run
it on the isolated rerun output via output_path=<isolated_output>, and on
explore's output when in doubt; refs resolve against that output's own
directory) — a missing/empty/escaping target is a self_consistency FAIL
(the output claims a file that isn't there). Soften the rest:
long-`content` reproducibility is judged at length/prefix level (TOLERANT
strings count as within tolerance when length drift is small and the
opening matches — compare_output applies this itself), and field_semantics
for `content`/`file_ref` asks "is this really the unit selectors.yaml's
`semantic` declares" against the live page, not value equality. "The unit could have been split into richer fields" is advisory
feedback, never a FAIL.

## Safety floor — the ONLY mechanical hard-fails
update_scorecard re-derives three conditions from DISK and will REJECT any
attempt to record the dim pass/n-a while they hold (you must record fail):
- `syntax` — workflow.py doesn't ast.parse.
- `runs_clean` — the isolated rerun CRASHED (non-zero exit / Python traceback).
  A rerun TIMEOUT is NOT a crash: a correct-but-slow crawl on a complex/
  paginated output is your call — usually not_verified, not fail.
- `secrets_safe` — a credential literal leaks into a shipped artifact.
EVERYTHING ELSE is your judgment: the check tools hand you facts, YOU decide
the status (including n/a for the validator artifacts above). There is no other
mechanical gate — the verdict is your gate-computed result over the statuses
you record.

## Output-location (advisory, not a hard gate)
workflow.py SHOULD write output_sample.json at its cwd ROOT. If
run_workflow_isolated returns a non-empty stray_output_samples (the file is
only below a subdirectory), weigh it: a normal extraction that misplaces its
output usually warrants runs_clean=fail + append_feedback to fix the path — but
a media / weak-schema deliverable may legitimately nest its files. You decide;
it is no longer a mechanical rejection.

## Complex outputs — data defect vs validator artifact (n/a, not fail)
A gating check can fail for two very different reasons: the harvested DATA is
wrong, OR the validator's re-fetch / re-run couldn't reproduce a legitimately
COMPLEX output. The second is a tooling limitation, not a defect, and must NOT
hard-FAIL the run. Telltales:
- your re-fetch finds the records share a listing/search source_url and the
  deeper ones aren't on the first paint (deep pagination / API / infinite
  scroll), while the records you COULD locate matched: the data is fine, the URL
  just isn't a per-record page → resample_match n/a (re-fetchability limit), not
  fail. BETTER: re-call the paginated API in browser_evaluate (or run_python) to
  fetch them ALL and actually verify — then it's a real pass.
- field_semantics where source_url is a listing/API endpoint with no per-record
  DOM element (record_locator won't resolve) — the value came from an API, not
  the rendered page.
- strict mismatches confined to VOLATILE fields (price, rank, "N ago", stock)
  that legitimately move between harvest and re-fetch.
task_plan.md (Explore's run plan — intended data / fields / crawl-method /
expectations) is your CONTEXT for telling these apart: if it says the crawl
pages an API by offset, or that source_url is a shared listing/search page,
a partial resample is the EXPECTED shape (an artifact), not a defect. But
task_plan is Explore's SELF-REPORT, not an independent basis — it may INFORM an
artifact classification, it may NEVER by itself justify a PASS or a rescue
(that still needs the corroborating independent dim below). A workflow that
plans badly still fails on the live-page / rerun basis.

When the evidence shows a validator artifact, record the dim as **n/a** with the
reason in the basis — NOT fail. GUARDRAIL (non-negotiable): rescue a gating dim
to n/a ONLY when an INDEPENDENT gating dim corroborates the data is sound
(reproducibility passed on the cold rerun, or field_semantics passed on a
located record). Never rescue a dim in isolation; never rescue when the failure
could be a real defect. Every artifact rescue MUST start its basis with
"RESCUED: " and the corroborating dim — this is cross-examined downstream, so an
unjustified rescue will be caught and reverted. Always append_feedback so the
next explore gives each record a re-fetchable source_url (its detail page / the
exact paginated request).

## Discipline
hard-FAIL only on a gating dim with an independent basis. "fields seem
incomplete / could be better" → append_feedback (advisory), NEVER a fail.
Read workflow.py source only as an investigative LEAD (e.g. to explain a
sparse-field absence); judge by output / re-fetch, never by how the code reads.
A re-fetch/re-run LIMITATION on a complex output is an artifact → n/a (see
above), not a defect → fail. When in genuine doubt between defect and artifact,
prefer not_verified (inconclusive) over a false fail.
"""


# Inline system prompt for DOWNLOAD workflows (file data sources). Used when
# the run produced download_manifest.yaml instead of selectors.yaml.
_DOWNLOAD_SYSTEM_PROMPT = """\
You are an autonomous validator agent for a web-crawler DOWNLOAD workflow.
workflow.py fetches file(s) (a downloadable dataset — CSV/JSON/XLSX/parquet/…)
to disk; you judge whether it satisfies goal.md by recording each dimension in
a scorecard whose `overall` is GATE-COMPUTED — you do NOT decide ad-hoc.

## Hard isolation
READ-ONLY on every explore artifact. Your tools write ONLY validation/<run_id>/.
Re-running goes through run_workflow_isolated (isolated rerun/ workdir; the
workflow's downloads land in rerun/<output_dir>/, NEVER overwriting explore's
files). No general Bash/Write/Edit.

## Your tools (all exposed as mcp__validator__*)
Checks: check_workflow_syntax, run_workflow_isolated, verify_downloaded_files,
run_python. Scorecard/feedback: init_scorecard, update_scorecard, read_scorecard,
write_report, append_feedback. Plus Read (read-only) for goal.md / any file.
run_python is your general escape hatch — write+run Python in an isolated work/
dir (scrubbed_env, no secrets) to verify ANY dimension your own way: re-parse the
downloaded bytes (csv/json/pyarrow/openpyxl), sniff content-type / gunzip, re-fetch
the source with httpx, hash/diff. The rerun's downloaded files are staged READ-ONLY
under work/inputs_ro/rerun/, so verify_downloaded_files is the PRIMARY probe for
file_downloaded/non_empty/format_valid/parses — never the sole basis; corroborate
anything ambiguous in run_python.

## Dimensions + the gate
7 dimensions: 6 GATING (syntax, runs_clean, file_downloaded, non_empty,
format_valid, parses) + 1 NON-GATING (completeness=loop-only). overall = fail
if any gating dim fails; pass if all gating dims are pass/"n/a"; otherwise
inconclusive. The workflow DECLARES what it fetched in download_manifest.yaml;
verify_downloaded_files checks the actual bytes against it.

## Discipline
The basis is the ACTUAL DOWNLOADED FILE (independent of the workflow's claims).
verify_downloaded_files reports per-file FACTS (exists / non_empty / format_valid
/ parses) — your PRIMARY evidence, NOT the verdict itself. A declared file
reported missing / empty / wrong-format / unparseable is a strong FAIL LEAD:
confirm it's the declared deliverable (not a path/manifest mismatch — check
run_workflow_isolated's produced_files, or re-read the staged bytes in run_python)
before you record the dim fail. YOU synthesize each status; only syntax /
runs_clean-crash / secrets-leak are mechanically floored — these file dims are
your judgment. `parses` may be `not_verified` when no parser exists for the
format (e.g. parquet without pyarrow) — tolerated, NOT pass. "Did it get ALL
files the source offers" is completeness → advisory / multi-round loop, NEVER a
1-pass FAIL. If the source turns out to be embedded page data (no real file to
download), emit INCONCLUSIVE — a download workflow can't validate a non-file.
"""


_API_SYSTEM_PROMPT = """\
You are an autonomous validator agent for an API crawl workflow. workflow.py
calls HTTP endpoint(s) and produces records (output_sample.json, like an
extraction workflow); you judge whether that satisfies goal.md by recording
each dimension in a scorecard whose `overall` is GATE-COMPUTED — you do NOT
decide the verdict ad-hoc.

## Hard isolation
READ-ONLY on every explore artifact (workflow.py / helpers.py /
api_manifest.yaml / output_sample.json / hypotheses.yaml / exploration_log.md).
Your tools write ONLY validation/<run_id>/. No general Bash/Write/Edit.
Re-running goes through run_workflow_isolated (isolated rerun/ workdir with
credentials.json copied in; NEVER overwrites explore's output_sample.json).

## Your tools (all exposed as mcp__validator__*)
Checks: check_workflow_syntax, run_workflow_isolated, run_python,
compare_field_names, compare_output, verify_file_refs, probe_api_endpoint,
check_no_secret_leak. Scorecard/feedback: init_scorecard, update_scorecard,
read_scorecard, write_report, append_feedback. Plus Read (read-only) for
goal.md / any file. run_python is your general escape hatch — write+run Python
(scrubbed_env, httpx) to re-call or paginate any endpoint, parse, hash or diff,
so you can corroborate endpoint_match / reproducibility your own way; the
tools' `→ dim` arrows are the PRIMARY probe, NOT the sole basis. The rerun's
produced output is staged READ-ONLY under work/inputs_ro/rerun/.
Records may be weak-schema data units (huge `content`, or `file_ref` to a
file the workflow saved — e.g. an attachment fetched over HTTP): few/large
records are LEGAL, and verify_file_refs checks every file_ref target exists
+ is non-empty (missing/empty → self_consistency FAIL; applicable=false →
no record carries file_ref, nothing to record).

## Dimensions + the gate
8 dimensions: 6 GATING (syntax, runs_clean, self_consistency, reproducibility,
endpoint_match, secrets_safe) + 2 NON-GATING (field_reasonableness=advisory,
completeness=loop-only). overall = fail if any gating dim fails; pass if all
gating dims are pass/"n/a"; otherwise inconclusive.

## What you can / can't verify
The LIVE API is your sharpest basis: probe_api_endpoint re-calls the
manifest's declared probe_url and reports which output values appear in the
real response (value_match) plus a raw_preview of the body — a LEAD, not a
verdict. probe_api_endpoint makes ONE call; to corroborate or go deeper, re-call
/ paginate the API yourself in run_python (httpx, scrubbed_env) to fetch more
rows, and judge endpoint_match from your synthesis of probe + your own re-call +
record structure, never from one probe's value_match alone. A low hit ratio
is NOT an auto-fail — workflows legitimately transform values (epoch→ISO
dates, unit conversions, joined fields); judge endpoint_match from the
raw_preview + record structure, and FAIL only when the output is clearly not
derivable from the live response (wrong endpoint, fabricated fields, stale
copy). secrets_safe: check_no_secret_leak returning na=true (no credentials —
open API) → record "n/a"; any leak in a SHIPPED artifact → fail (its
advisory_leaks list does NOT gate — surface via append_feedback instead).
You CANNOT hard-verify completeness — advisory + multi-round loop only.

## Output-location (advisory, not a hard gate)
workflow.py SHOULD write output_sample.json at its cwd ROOT. If
run_workflow_isolated returns a non-empty stray_output_samples (the file is
only below a subdirectory), weigh it: a normal API workflow that misplaces its
output usually warrants runs_clean=fail + append_feedback to fix the path — but
a media / weak-schema deliverable may legitimately nest its files. You decide;
it is no longer a mechanical rejection.

## Discipline
hard-FAIL only on a gating dim with an independent basis. NEVER print a
credential value (tool outputs arrive pre-masked; keep them masked). A
401/403 from the probe usually means a missing/expired user key — that's
INCONCLUSIVE territory with clear feedback, not a code-bug FAIL, unless
workflow.py itself mishandles auth. Read workflow.py source only as an
investigative lead; judge by output / live response, never by how code reads.
"""


@dataclass
class ValidationSummary:
    """End-of-validation aggregate. `verdict` is parsed from the session's
    final line and should equal the scorecard's computed `overall`."""

    site_id: str | None = None
    workflow_type: str = "extraction"  # extraction | download | api
    verdict: str = "UNSET"  # PASS | FAIL | INCONCLUSIVE | ERROR | UNSET
    verdict_line: str | None = None
    run_id: str | None = None
    reason: str | None = None
    turn_count: int = 0
    tool_calls: int = 0
    tool_call_breakdown: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    finished: bool = False
    error: str | None = None
    run_log_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "workflow_type": self.workflow_type,
            "verdict": self.verdict,
            "verdict_line": self.verdict_line,
            "run_id": self.run_id,
            "reason": self.reason,
            "turn_count": self.turn_count,
            "tool_calls": self.tool_calls,
            "tool_call_breakdown": dict(self.tool_call_breakdown),
            "total_cost_usd": self.total_cost_usd,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "finished": self.finished,
            "error": self.error,
            "run_log_path": self.run_log_path,
        }


def _parse_verdict_line(text: str):
    """Return (verdict, site_id, run_id, reason) from the LAST matching line."""
    if not text:
        return None
    for line in reversed(text.splitlines()):
        m = VERDICT_LINE_RE.search(line.strip())
        if m:
            return (m.group(1).upper(), m.group(2), m.group(3), (m.group(4) or "").strip() or None)
    return None


def _reconcile_verdict(summary: ValidationSummary, root: Path, assigned_run_id: str) -> None:
    """Mechanical reconciliation — the verdict line is the agent's CLAIM; the
    scorecard's computed overall plus the on-disk output contract are the
    truth. A PASS the disk doesn't back never reaches the orchestrator.

    - summary.run_id is pinned to the ASSIGNED run id (the line's run_id is a
      claim, not an identity — it must not redirect which scorecard is read).
    - Conservative claims (FAIL / INCONCLUSIVE / ERROR) are never upgraded.
    - PASS with a missing/corrupt scorecard → INCONCLUSIVE (unbacked claim).
    - PASS with overall != pass → clamped to the scorecard's own verdict.
    - PASS with overall == pass → trusted. The SAFETY FLOOR (syntax parse-fail /
      runs_clean crash / secrets shipped-leak) is enforced at WRITE time by
      update_scorecard, so an unbacked PASS can't be assembled here. The
      output-location contract is advisory now — no consumption-time revoke."""
    from mcp_server.schemas import ScorecardFile

    summary.run_id = assigned_run_id
    if summary.verdict != "PASS":
        return
    val_dir = root / "workspaces" / (summary.site_id or "") / "validation" / assigned_run_id
    try:
        overall = ScorecardFile.load(val_dir / "scorecard.yaml").overall
    except Exception:  # noqa: BLE001 — missing/corrupt scorecard = unbacked PASS
        overall = None
    if overall != "pass":
        downgraded = "FAIL" if overall == "fail" else "INCONCLUSIVE"
        summary.verdict = downgraded
        summary.reason = (
            f"verdict line claimed PASS but scorecard overall is "
            f"{overall or 'missing'} — downgraded to {downgraded}."
            + (f" Agent reason: {summary.reason}" if summary.reason else "")
        )
        return
    # overall == pass AND the safety floor held (enforced by update_scorecard at
    # write time) → trust the agent's PASS. A misplaced output file is advisory
    # now — the agent's call via runs_clean, not a consumption-time revoke.


def _build_prompt(site_id: str, run_id: str) -> str:
    return (
        f"Validate site '{site_id}'. run_id = {run_id}.\n\n"
        f"Judge whether the latest workflow.py output satisfies "
        f"inputs/{site_id}/goal.md. Record every dimension in the scorecard; "
        f"the verdict is gate-computed from it.\n\n"
        f"Suggested spine (adapt freely — you decide order/depth/which tools; the "
        f"`→ dim` arrows are the PRIMARY probe, NOT the sole basis — pull other tools' "
        f"evidence into each dim and corroborate, per 'Evidence is cross-cutting'):\n"
        f"  1. init_scorecard(site_id, run_id)\n"
        f"  2. Read inputs/{site_id}/goal.md (the contract) + workspaces/{site_id}/task_plan.md "
        f"(Explore's intended crawl-method/expectations — CONTEXT for telling a validator artifact "
        f"from a real defect; it is Explore's self-report, NEVER a basis for PASS on its own)\n"
        f"  3. check_workflow_syntax → update_scorecard('syntax', ...)\n"
        f"  4. run_workflow_isolated(run_id) → update_scorecard('runs_clean', ...) "
        f"(note its isolated_output path for the next two)\n"
        f"  5. compare_field_names(output_path=<isolated_output>) → 'self_consistency'\n"
        f"     (records carry file_ref? verify_file_refs(output_path=<isolated_output>) "
        f"folds into 'self_consistency'; applicable=false → skip silently)\n"
        f"  6. compare_output(rerun_output_path=<isolated_output>) → 'reproducibility'\n"
        f"  7. resample_match — verify it YOURSELF: drive the browser to the right place "
        f"(browser_goto / browser_content / browser_evaluate) — e.g. re-call the site's "
        f"paginated API via fetch() in browser_evaluate, or open a detail page — re-extract, "
        f"and compare to output_sample.json; or do it in run_python (httpx). Record the "
        f"verdict (n/a when the records aren't independently re-fetchable — a limitation, not "
        f"a defect)\n"
        f"  8. inspect_record_html(run_id, source_url, selector=<suspect field's>) → judge "
        f"semantics → 'field_semantics'\n"
        f"  9. (optional) 'field_reasonableness'/'completeness' advisory; write_report; "
        f"append_feedback on gaps\n"
        f" 10. read_scorecard → take its `overall` → emit the verdict line\n\n"
        f"**Verdict contract** (orchestrator parses via regex — verbatim):\n\n"
        f"    [PASS|FAIL|INCONCLUSIVE|ERROR] {site_id} run_id={run_id} — <one-line reason>\n\n"
        f"PASS/FAIL/INCONCLUSIVE MUST equal read_scorecard's `overall`. ERROR only if a "
        f"facts collector itself crashes. This MUST be your LAST line."
    )


def _detect_workflow_type(site_id: str, root: Path) -> str:
    """Workflow type from workspace artifacts, with an input-side api fallback.

    The artifact the explore agent wrote declares the type (selectors.yaml →
    extraction, download_manifest.yaml → download, api_manifest.yaml → api);
    api wins over download wins over extraction if several exist. When NO
    declaring artifact exists yet but the site was staged as an API source
    (inputs/<site>/api_spec.json), fall back to `api` — so a first iter that
    crashed before writing its manifest still gets the api validator (which
    errors informatively → INCONCLUSIVE) instead of a nonsense extraction FAIL.
    """
    ws = root / "workspaces" / site_id
    if (ws / "api_manifest.yaml").exists():
        return "api"
    if (ws / "download_manifest.yaml").exists():
        return "download"
    if (root / "inputs" / site_id / "api_spec.json").exists():
        # API-staged site with no declaring artifact: stay in the api lane even
        # if a stray selectors.yaml exists (api→extraction pivots are forbidden —
        # the browser is disabled for api runs).
        return "api"
    return "extraction"


def _build_download_prompt(site_id: str, run_id: str) -> str:
    return (
        f"Validate the DOWNLOAD workflow for site '{site_id}'. run_id = {run_id}.\n\n"
        f"Judge whether the latest workflow.py downloads the file(s) "
        f"inputs/{site_id}/goal.md requires. The workflow declared them in "
        f"download_manifest.yaml; the verdict is gate-computed from the scorecard.\n\n"
        f"Suggested spine (adapt freely — the `→ dim` arrows are the PRIMARY probe, "
        f"NOT the sole basis: verify_downloaded_files reports facts about the bytes, but "
        f"YOU decide each dim; corroborate anything ambiguous with run_workflow_isolated's "
        f"produced_files or your own run_python re-read/parse/hash of the staged rerun file "
        f"before recording a gating dim):\n"
        f"  1. init_scorecard(site_id, run_id, workflow_type='download')\n"
        f"  2. Read inputs/{site_id}/goal.md (the contract) + workspaces/{site_id}/task_plan.md "
        f"(Explore's intended crawl-method/expectations — CONTEXT for telling a validator artifact "
        f"from a real defect; it is Explore's self-report, NEVER a basis for PASS on its own)\n"
        f"  3. check_workflow_syntax → 'syntax'\n"
        f"  4. run_workflow_isolated(run_id) → 'runs_clean' (downloads land in rerun/)\n"
        f"  5. verify_downloaded_files(run_id) → its aggregate is EVIDENCE (not a verdict) toward "
        f"'file_downloaded' / 'non_empty' / 'format_valid' / 'parses' (a null parse = not_verified, "
        f"NOT fail); cross-check anything ambiguous in run_python (re-parse / sniff / hash the staged "
        f"rerun file)\n"
        f"  6. (optional) 'completeness' advisory; write_report; append_feedback on gaps\n"
        f"  7. read_scorecard → take its `overall` → emit the verdict line\n\n"
        f"**Verdict contract** (orchestrator parses via regex — verbatim):\n\n"
        f"    [PASS|FAIL|INCONCLUSIVE|ERROR] {site_id} run_id={run_id} — <one-line reason>\n\n"
        f"PASS/FAIL/INCONCLUSIVE MUST equal read_scorecard's `overall`. ERROR only if a "
        f"check itself crashes. This MUST be your LAST line."
    )


def _build_api_prompt(site_id: str, run_id: str) -> str:
    return (
        f"Validate the API workflow for site '{site_id}'. run_id = {run_id}.\n\n"
        f"Judge whether the latest workflow.py's records satisfy "
        f"inputs/{site_id}/goal.md. The workflow declared its endpoints + fields in "
        f"api_manifest.yaml; the verdict is gate-computed from the scorecard.\n\n"
        f"Suggested spine (adapt freely — the `→ dim` arrows are the PRIMARY probe, "
        f"not the sole basis; corroborate a fail/ambiguous signal with another tool "
        f"before you record a dim):\n"
        f"  1. init_scorecard(site_id, run_id, workflow_type='api')\n"
        f"  2. Read inputs/{site_id}/goal.md (the contract) + workspaces/{site_id}/api_manifest.yaml "
        f"(note its identifier_field) + workspaces/{site_id}/task_plan.md (Explore's intended "
        f"crawl-method/expectations — CONTEXT for telling a validator artifact from a real defect; "
        f"Explore's self-report, NEVER a basis for PASS on its own)\n"
        f"  3. check_workflow_syntax → 'syntax'\n"
        f"  4. run_workflow_isolated(run_id) → 'runs_clean' (credentials.json is copied into "
        f"rerun/; note its isolated_output path for the next two)\n"
        f"  5. compare_field_names(output_path=<isolated_output>) → 'self_consistency'\n"
        f"     (records carry file_ref? verify_file_refs(output_path=<isolated_output>) "
        f"folds into 'self_consistency'; applicable=false → skip silently)\n"
        f"  6. compare_output(rerun_output_path=<isolated_output>, identifier_field=<manifest's>) "
        f"→ 'reproducibility'\n"
        f"  7. probe_api_endpoint(run_id) → judge value_match + raw_preview → 'endpoint_match' "
        f"(transformed values are legitimate — fail only when the output is clearly not "
        f"derivable from the live response)\n"
        f"  8. check_no_secret_leak(run_id) → 'secrets_safe' (na=true → record n/a)\n"
        f"  9. (optional) 'field_reasonableness'/'completeness' advisory; write_report; "
        f"append_feedback on gaps\n"
        f" 10. read_scorecard → take its `overall` → emit the verdict line\n\n"
        f"**Verdict contract** (orchestrator parses via regex — verbatim):\n\n"
        f"    [PASS|FAIL|INCONCLUSIVE|ERROR] {site_id} run_id={run_id} — <one-line reason>\n\n"
        f"PASS/FAIL/INCONCLUSIVE MUST equal read_scorecard's `overall`. ERROR only if a "
        f"check itself crashes. This MUST be your LAST line."
    )


def _build_options(
    root: Path,
    *,
    permission_mode: str,
    max_turns: int,
    model: str | None,
    workflow_type: str = "extraction",
) -> ClaudeAgentOptions:
    """Self-contained options: validator MCP server + Read only, enforced by
    tools=["Read"] — no Bash/Write/Edit/Skill in the built-in set (isolation
    by tool surface; allowed_tools alone restricts nothing). The system prompt
    + allowed tool subset are chosen by workflow_type (extraction/download/api)."""
    from runtime.validator_tools import build_validator_server

    if workflow_type == "download":
        system_prompt, tool_names = _DOWNLOAD_SYSTEM_PROMPT, _DOWNLOAD_TOOL_NAMES
    elif workflow_type == "api":
        system_prompt, tool_names = _API_SYSTEM_PROMPT, _API_TOOL_NAMES
    else:
        system_prompt, tool_names = _SYSTEM_PROMPT, _TOOL_NAMES
    kwargs: dict[str, Any] = {
        "cwd": str(root),
        "system_prompt": system_prompt,
        "permission_mode": permission_mode,
        "max_turns": max_turns,
        "mcp_servers": {"validator": build_validator_server()},
        # tools (NOT allowed_tools) is what actually restricts the built-in
        # tool surface: allowed_tools is only the no-prompt allowlist — under
        # bypassPermissions alone, Bash/Write/Edit would all be live and a
        # validator could rewrite scorecard.yaml around the update_scorecard
        # hard gate. MCP tools ride mcp_servers and are unaffected.
        "tools": ["Read"],
        "allowed_tools": ["Read"] + [f"mcp__validator__{t}" for t in tool_names],
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


def _msg_events(msg: Any, summary: ValidationSummary) -> list[dict[str, Any]]:
    """Normalize one SDK message into small event dicts + update summary."""
    out: list[dict[str, Any]] = []
    if isinstance(msg, SystemMessage):
        out.append({"kind": "system", "subtype": getattr(msg, "subtype", None)})
    elif isinstance(msg, AssistantMessage):
        summary.turn_count += 1
        for b in msg.content:
            if isinstance(b, TextBlock):
                out.append({"kind": "assistant_text", "text": b.text})
            elif isinstance(b, ToolUseBlock):
                summary.tool_calls += 1
                summary.tool_call_breakdown[b.name] = summary.tool_call_breakdown.get(b.name, 0) + 1
                out.append({"kind": "tool_use", "name": b.name, "id": b.id, "input": b.input})
    elif isinstance(msg, UserMessage):
        for b in msg.content if isinstance(msg.content, list) else []:
            if isinstance(b, ToolResultBlock):
                out.append(
                    {
                        "kind": "tool_result",
                        "tool_use_id": getattr(b, "tool_use_id", None),
                        "content": getattr(b, "content", None),
                        "is_error": getattr(b, "is_error", False),
                    }
                )
    elif isinstance(msg, ResultMessage):
        summary.finished = True
        usage = getattr(msg, "usage", {}) or {}
        # DeepSeek runs: recompute cost from usage (the SDK overstates ~25-40x).
        summary.total_cost_usd = pricing.effective_cost(usage, getattr(msg, "total_cost_usd", None))
        summary.total_input_tokens = usage.get("input_tokens")
        summary.total_output_tokens = usage.get("output_tokens")
        out.append({"kind": "result", "cost_usd": summary.total_cost_usd})
    return out


async def validate(
    site_id: str,
    *,
    run_id: str | None = None,
    project_root: Path | str | None = None,
    permission_mode: str = "bypassPermissions",
    max_turns: int = 40,
    model: str | None = None,
    summary: ValidationSummary | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Spawn a self-contained validator SDK session. Yields normalized event
    dicts; updates `summary` in place (verdict/run_id/reason from the final
    verdict line, which should equal the scorecard's computed overall)."""
    if summary is None:
        summary = ValidationSummary()
    summary.site_id = site_id
    run_id = run_id or f"val-{uuid.uuid4().hex[:8]}"
    summary.run_id = run_id

    assert_api_key()
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent
    workflow_type = _detect_workflow_type(site_id, root)
    summary.workflow_type = workflow_type
    options = _build_options(
        root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        workflow_type=workflow_type,
    )
    if workflow_type == "download":
        prompt = _build_download_prompt(site_id, run_id)
    elif workflow_type == "api":
        prompt = _build_api_prompt(site_id, run_id)
    else:
        prompt = _build_prompt(site_id, run_id)

    # Per-session JSONL run log (unconditional) — mirrors the explore side so
    # the validator's full tool trace + per-turn model + tracebacks survive
    # without depending on stdout capture.
    rl = RunLogger(
        role="validate",
        site_id=site_id,
        config={
            "model": model,
            "max_turns": max_turns,
            "run_id": run_id,
            "workflow_type": workflow_type,
            "permission_mode": permission_mode,
        },
    )
    summary.run_log_path = str(rl.path)

    chunks: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                rl.write(
                    {
                        "event": "agent_turn",
                        "turn": summary.turn_count + 1,
                        "model": getattr(msg, "model", None),
                        "usage": jsonable_usage(getattr(msg, "usage", None)),
                    }
                )
            for ev in _msg_events(msg, summary):
                if ev["kind"] == "assistant_text":
                    chunks.append(ev["text"])
                rl.event(ev)
                yield ev
        rl.write({"event": "run_complete", "summary": summary.to_dict()})
    except Exception as exc:  # noqa: BLE001 — surface as event
        summary.error = f"{type(exc).__name__}: {exc}"
        rl.write(
            {
                "event": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        yield {"kind": "error", "error": summary.error}

    combined = "\n".join(chunks).rstrip()
    parsed = _parse_verdict_line(combined)
    if parsed is not None:
        verdict, parsed_site, parsed_run, reason = parsed
        summary.verdict = verdict
        summary.verdict_line = next(
            (ln for ln in reversed(combined.splitlines()) if VERDICT_LINE_RE.search(ln)), None
        )
        summary.reason = reason
        if parsed_site and parsed_site != site_id:
            summary.error = (
                f"verdict line site_id mismatch: contract said '{site_id}', "
                f"model wrote '{parsed_site}'"
            )
        if parsed_run and parsed_run != run_id:
            summary.error = (
                summary.error + " | " if summary.error else ""
            ) + f"verdict line run_id mismatch: assigned '{run_id}', model wrote '{parsed_run}'"
    else:
        summary.verdict = "INCONCLUSIVE"
        summary.reason = "no verdict line emitted by validation session"
    # The line is a claim; the disk is the truth (pins run_id, clamps PASS).
    _reconcile_verdict(summary, root, run_id)


__all__ = ["ValidationSummary", "validate"]
