"""Self-contained Data Agent driven by the Claude Agent SDK.

The pipeline's 5th stage — the *consumption* stage. Where validate/run are
locked-down (their entire tool surface is a narrow in-process MCP server), the
Data Agent is the INVERSE: a FULL-capability ``claude_code`` session (Read /
Write / Edit / Bash / Glob / Grep / WebFetch + skills), grounded on the real
crawled data, with an ADDITIVE ``data`` MCP server for convenience + audit. Its
closest template is ``runtime/run.py::explore`` (built via ``options.build_options``),
not the validator.

It cleans one or more user-selected crawled datasets (cleaning is USER-DIRECTED
+ audited) and builds open-ended data products (reports, charts, derived
datasets, decks, spreadsheets, ...). Isolation is by DIRECTORY CONVENTION: the
selected sources are staged read-only into ``products/<product_id>/sources/``
before the session, and the agent writes only under ``products/<product_id>/``.

The completion ``outcome`` is GATE-COMPUTED in the manifest (see
``mcp_server.schemas.ProductManifestFile``) — it judges completion, never
product quality. The session's final line must be the outcome contract:

    [COMPLETE|PARTIAL|FAILED|ABORTED] product_id=<prod-XXXX> sources=<N> products=<M> — <reason>

which equals the manifest's computed ``outcome`` (ERROR is the only system-layer
exception). ``runtime.cli`` parses this line.
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
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from runtime import pricing
from runtime.options import assert_api_key, build_options
from runtime.run_logger import RunLogger, jsonable_usage
from runtime.data_prep import stage_sources
from runtime.data_tools import build_data_server
from mcp_server.schemas import ProductManifestFile, SourceRef, new_product_manifest


OUTCOME_LINE_RE = re.compile(
    r"\[(COMPLETE|PARTIAL|FAILED|ABORTED)\]\s+product_id=(\S+)"
    r"(?:\s+sources=(\d+))?(?:\s+products=(\d+))?\s*(?:[—-]\s*(.*))?",
    re.IGNORECASE,
)


@dataclass
class DataAgentSummary:
    """End-of-run aggregate. `outcome` comes from the manifest's computed gate
    (authoritative); the parsed final line should equal it."""

    product_id: str | None = None
    task: str | None = None
    outcome: str = "UNSET"  # COMPLETE | PARTIAL | FAILED | ABORTED | ERROR | UNSET
    outcome_line: str | None = None
    sources_count: int = 0  # sources that staged ok
    products_count: int = 0  # registered non-empty products
    reason: str | None = None
    output_root: str | None = None
    control_dir: str | None = None
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
            "product_id": self.product_id,
            "task": self.task,
            "outcome": self.outcome,
            "outcome_line": self.outcome_line,
            "sources_count": self.sources_count,
            "products_count": self.products_count,
            "reason": self.reason,
            "output_root": self.output_root,
            "control_dir": self.control_dir,
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


def _parse_outcome_line(text: str):
    """Return (outcome, product_id, sources, products, reason) from the LAST
    matching line, or None."""
    if not text:
        return None
    for line in reversed(text.splitlines()):
        m = OUTCOME_LINE_RE.search(line.strip())
        if m:
            return (
                m.group(1).upper(),
                m.group(2),
                int(m.group(3)) if m.group(3) else None,
                int(m.group(4)) if m.group(4) else None,
                (m.group(5) or "").strip() or None,
            )
    return None


def _staged_lines(staged: list[dict]) -> str:
    lines = []
    for s in staged:
        where = s.get("staged_path") or f"in-place: {s.get('source_path')}"
        need = f" | user_need: {s['user_need']}" if s.get("user_need") else ""
        note = f" | note: {s['note']}" if s.get("note") else ""
        lines.append(
            f"  - token={s['token']} site_id={s.get('site_id')} type={s['type']} "
            f"records={s.get('record_count')} ok={s.get('ok')} → {where}{need}{note}"
        )
    return "\n".join(lines) if lines else "  (none resolved)"


def _build_prompt(
    product_id: str,
    task: str,
    staged: dict,
    clean_spec_text: str | None,
) -> str:
    data_dir = staged["data_dir"]
    control_dir = staged["control_dir"]
    clean_block = (
        f"\n## Cleaning instructions (USER-DIRECTED — do exactly this, nothing more)\n{clean_spec_text}\n"
        if clean_spec_text
        else "\n## Cleaning\nNo cleaning spec was given. Do NOT invent destructive cleaning — load the "
        "data as-is (at most lossless normalization). Note 'no cleaning spec' in the report.\n"
    )
    return f"""\
You are the **Data Agent** — the pipeline's data-processing + data-product stage. You have the
FULL Claude Code toolset (Read/Write/Edit/Bash/Glob/Grep/WebFetch + skills) PLUS the
`mcp__data__*` convenience tools. You are "Claude Code, grounded on real crawled data": build
whatever the task asks, using the staged datasets as the factual substrate.

product_id = **{product_id}** — pass it to every `mcp__data__*` tool.

## Where things are
- Data dir: `{data_dir}` (sources/ = staged READ-ONLY source copies; clean/ = your cleaned data;
  products/ = your deliverables). Control dir: `{control_dir}` (manifest / recipe / report).
- Staged sources:
{_staged_lines(staged["sources"])}
- Write ALL outputs under `{data_dir}/products/` (and cleaned data under `clean/`). NEVER modify
  anything in `sources/` or elsewhere in the repo.

## Task (what to build)
{task}
{clean_block}
## How to work
1. `profile_dataset(product_id, path)` each source BEFORE reasoning about it — never read a whole
   dataset into context. Do full-data computation with CODE (write Python via Bash; pandas/
   matplotlib/openpyxl/python-docx can be `pip install`ed into .venv if a product needs them).
2. Clean ONLY as instructed: prefer `apply_cleaning` (writes clean/ + an auditable recipe) for the
   listed ops; for anything it can't express, write pandas and log it with `record_cleaning_step`.
   To combine sources, use `merge_datasets`.
3. Build the product(s) under `products/`. For EACH deliverable call
   `register_product(product_id, path, kind, title)` (kind = report|dataset|chart|spreadsheet|
   deck|document|other). A non-empty registration flips the completion gate.
4. `write_product_report(product_id, section)` — a short narrative (sources, cleaning, products).
5. If the data was INADEQUATE for the request (missing field, too sparse), `append_feedback(...)`.
6. `read_product_manifest(product_id)` → take its `outcome` → emit the outcome line LAST.

## Outcome gate (completion, NOT quality)
Gating dims: sources_loaded, products_produced, within_budget. Quality of the product is the human
reader's call — you are judged only on producing non-empty deliverables from non-empty sources.

## Outcome contract (orchestrator parses via regex — verbatim, your LAST line)

    [COMPLETE|PARTIAL|FAILED|ABORTED] product_id={product_id} sources=<N> products=<M> — <one-line reason>

COMPLETE/PARTIAL/FAILED/ABORTED MUST equal read_product_manifest's `outcome`.
"""


def _build_options(
    root: Path,
    *,
    permission_mode: str,
    max_turns: int,
    model: str | None,
    setting_sources: list[str],
    load_mcp_json: bool,
    data_config: dict,
):
    """Full-capability options (preset claude_code + skills + default tools) with
    the additive `data` MCP server. The INVERSE of validate/run's narrow surface."""
    built = build_options(
        project_root=root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        setting_sources=setting_sources,
        extra_mcp_servers={"data": build_data_server(data_config)},
        load_mcp_json=load_mcp_json,
    )
    return built.options


def _msg_events(msg: Any, summary: DataAgentSummary) -> list[dict[str, Any]]:
    """Normalize one SDK message into event dicts + update summary (same shape
    as runtime.run_agent._msg_events)."""
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
        summary.total_cost_usd = pricing.effective_cost(
            usage, getattr(msg, "total_cost_usd", None)
        )
        summary.total_input_tokens = usage.get("input_tokens")
        summary.total_output_tokens = usage.get("output_tokens")
        out.append({"kind": "result", "cost_usd": summary.total_cost_usd})
    return out


def _init_manifest(product_id: str, task: str, output_root: str | None, staged: dict) -> Path:
    """Create products/<id>/manifest.yaml with the staged sources + sources_loaded
    gate set, BEFORE the session. Returns the manifest path."""
    control = Path(staged["control_dir"])
    m = new_product_manifest(product_id, task=task, output_root=output_root)
    any_ok = False
    for s in staged["sources"]:
        any_ok = any_ok or bool(s.get("ok"))
        m.add_source(
            SourceRef(
                token=s["token"],
                site_id=s.get("site_id"),
                run_id=s.get("run_id"),
                type=s.get("type", "records"),
                staged_path=s.get("staged_path"),
                record_count=s.get("record_count"),
                field_count=s.get("field_count"),
                user_need=s.get("user_need"),
                ok=bool(s.get("ok")),
                note=s.get("note", ""),
            )
        )
    n_ok = sum(1 for s in staged["sources"] if s.get("ok"))
    if any_ok:
        m.set_dimension(
            "sources_loaded",
            "pass",
            basis="staged sources",
            evidence=f"{n_ok} non-empty source(s) staged",
        )
    else:
        m.set_dimension(
            "sources_loaded",
            "fail",
            basis="staged sources",
            evidence="no source resolved to a non-empty dataset",
        )
    control.mkdir(parents=True, exist_ok=True)
    m.save(control / "manifest.yaml")
    return control / "manifest.yaml"


def _finalize(manifest_path: Path, summary: DataAgentSummary) -> None:
    """Set within_budget from the session result, then read the authoritative
    gate outcome + counts from the manifest into the summary."""
    try:
        m = ProductManifestFile.load(manifest_path)
        if summary.error:
            m.set_dimension("within_budget", "fail", basis="session", evidence=summary.error[:200])
        else:
            m.set_dimension(
                "within_budget", "pass", basis="session", evidence="completed within turn budget"
            )
        m.save(manifest_path)
        summary.outcome = m.outcome.upper()
        summary.sources_count = sum(1 for s in m.sources if s.ok)
        summary.products_count = sum(1 for p in m.products if p.non_empty)
    except Exception as exc:  # noqa: BLE001 — manifest unreadable is itself a system error
        summary.outcome = "ERROR"
        summary.error = (summary.error or "") + f" | finalize: {type(exc).__name__}: {exc}"


async def data_agent(
    sources: list[str],
    task: str,
    *,
    product_id: str | None = None,
    clean_spec: str | None = None,
    project_root: Path | str | None = None,
    permission_mode: str = "bypassPermissions",
    max_turns: int = 60,
    model: str | None = None,
    output_root: str | None = None,
    setting_sources: list[str] | None = None,
    load_mcp_json: bool = False,
    sample_rows: int = 20,
    top_n: int = 15,
    max_field_chars: int = 200,
    summary: DataAgentSummary | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Spawn a full-capability Data Agent session over one or more crawled
    datasets. Yields normalized event dicts; updates `summary` in place. The
    outcome is read from the manifest's computed gate (authoritative)."""
    if summary is None:
        summary = DataAgentSummary()
    product_id = product_id or f"prod-{uuid.uuid4().hex[:8]}"
    summary.product_id = product_id
    summary.task = task
    summary.output_root = output_root

    assert_api_key()
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent

    # 1) Deterministic prep BEFORE the session: stage sources read-only + init manifest.
    staged = stage_sources(product_id, sources, output_root)
    summary.control_dir = staged["control_dir"]
    manifest_path = _init_manifest(product_id, task, output_root, staged)

    # 2) Optional cleaning spec file → text folded into the prompt.
    clean_spec_text: str | None = None
    if clean_spec:
        try:
            clean_spec_text = Path(clean_spec).read_text(encoding="utf-8")
        except OSError as exc:
            clean_spec_text = f"(could not read --clean-spec {clean_spec}: {exc})"

    data_config = {
        "output_root": output_root,
        "sample_rows": sample_rows,
        "top_n": top_n,
        "max_field_chars": max_field_chars,
    }
    options = _build_options(
        root,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        setting_sources=setting_sources or ["project"],
        load_mcp_json=load_mcp_json,
        data_config=data_config,
    )
    prompt = _build_prompt(product_id, task, staged, clean_spec_text)

    rl = RunLogger(
        role="data",
        site_id=None,
        config={
            "model": model,
            "max_turns": max_turns,
            "product_id": product_id,
            "sources": sources,
            "output_root": output_root,
            "setting_sources": setting_sources or ["project"],
            "load_mcp_json": load_mcp_json,
            "permission_mode": permission_mode,
            "task_preview": task[:200],
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

    # 3) Finalize: within_budget + read the authoritative gate outcome.
    _finalize(manifest_path, summary)

    combined = "\n".join(chunks).rstrip()
    parsed = _parse_outcome_line(combined)
    if parsed is not None:
        outcome, parsed_pid, n_src, n_prod, reason = parsed
        summary.outcome_line = next(
            (ln for ln in reversed(combined.splitlines()) if OUTCOME_LINE_RE.search(ln)), None
        )
        summary.reason = reason
        if outcome != summary.outcome and summary.outcome not in ("ERROR",):
            # Manifest gate is authoritative; record the disagreement, don't override.
            summary.error = (summary.error or "") + (
                f" | outcome line said {outcome} but manifest gate computed {summary.outcome}"
            )
        if parsed_pid and parsed_pid != product_id:
            summary.error = (summary.error or "") + (
                f" | outcome line product_id mismatch: expected {product_id}, got {parsed_pid}"
            )
    else:
        if summary.outcome == "UNSET":
            summary.outcome = "ERROR" if summary.error else "ABORTED"
        summary.reason = (
            summary.reason or summary.error or "no outcome line emitted by data session"
        )

    rl.write({"event": "run_complete", "summary": summary.to_dict()})


__all__ = ["DataAgentSummary", "data_agent"]
