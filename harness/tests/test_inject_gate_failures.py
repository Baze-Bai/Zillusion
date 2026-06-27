"""inject_memory_and_skills: the agentic/inline active gate-failure scaffold.

``run_gate_failures_lines`` is the agentic/inline analog of the deterministic
workflow.py-diff + task_plan↔selectors scaffolds: when the prior iter's harvest
run did NOT gate-compute COMPLETE, it ACTIVELY injects the FAILING gating
dimensions + their basis into the next iter's context (vs inject_run_feedback's
passive "go read manifest.yaml" pointer). These tests pin that behavior + the
per_workspace_sections wiring. No LLM — pure file I/O.

The hook lives at .claude/hooks/ (not an importable package), so we load it by
path, the same convention test_credentials_guard.py uses for HOOK.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

_HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "inject_memory_and_skills.py"
_spec = importlib.util.spec_from_file_location("inject_memory_and_skills_under_test", _HOOK)
ims = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ims)


_DIMS = {
    "launched": {"gating": True, "status": "pass", "basis": "ok"},
    "produced_output": {"gating": True, "status": "pass"},
    "self_consistency": {
        "gating": True,
        "status": "fail",
        "basis": "3 records missing source_url; file_ref media/p3.jpg missing",
    },
    "completeness": {
        "gating": True,
        "status": "not_verified",
        "basis": "processed 12/30 (40%) - DOWNGRADED from pass",
    },
    "secrets_safe": {"gating": True, "status": "pass"},
    "partial_failures": {"gating": False, "status": "advisory", "basis": "5 tombstoned"},
}


def _build_ws(parent: Path, *, route, outcome, dims=_DIMS, with_run=True, run_id="r-abc") -> Path:
    """Make <parent>/site-x/ with a crawl_route.json + (optionally) a run manifest."""
    ws = parent / "site-x"
    ws.mkdir(parents=True, exist_ok=True)
    if route is not None:
        (ws / "crawl_route.json").write_text(
            json.dumps({"route": route, "run_id": run_id}), encoding="utf-8"
        )
    if with_run:
        run = ws / "runs" / run_id
        run.mkdir(parents=True)
        (run / "manifest.yaml").write_text(
            yaml.safe_dump({"outcome": outcome, "dimensions": dims}), encoding="utf-8"
        )
    return ws


def test_inline_partial_injects_only_failing_gating_dims(tmp_path):
    ws = _build_ws(tmp_path, route="inline", outcome="partial")
    text = "\n".join(ims.run_gate_failures_lines(ws))
    assert "self_consistency" in text and "missing source_url" in text
    assert "completeness" in text and "DOWNGRADED" in text
    assert "launched" not in text  # a PASSING gating dim must not be surfaced
    assert "partial_failures" not in text  # an advisory (non-gating) dim must not be surfaced
    assert "pivot the route" in text
    assert "PARTIAL" in text


def test_complete_outcome_is_silent(tmp_path):
    ws = _build_ws(tmp_path, route="inline", outcome="complete")
    assert ims.run_gate_failures_lines(ws) == []


def test_deterministic_route_is_silent(tmp_path):
    # deterministic's iter signal is the workflow/validator scaffolds, not this.
    ws = _build_ws(tmp_path, route="deterministic", outcome="partial")
    assert ims.run_gate_failures_lines(ws) == []


def test_agentic_aborted_injects(tmp_path):
    ws = _build_ws(tmp_path, route="agentic", outcome="aborted")
    text = "\n".join(ims.run_gate_failures_lines(ws))
    assert "agentic" in text and "ABORTED" in text


def test_no_run_dir_is_silent(tmp_path):
    ws = _build_ws(tmp_path, route="inline", outcome="partial", with_run=False)
    assert ims.run_gate_failures_lines(ws) == []


def test_no_crawl_route_is_silent(tmp_path):
    ws = _build_ws(tmp_path, route=None, outcome="partial")  # run exists, no crawl_route.json
    assert ims.run_gate_failures_lines(ws) == []


def test_malformed_manifest_does_not_crash(tmp_path):
    ws = tmp_path / "site-x"
    run = ws / "runs" / "r-abc"
    run.mkdir(parents=True)
    (ws / "crawl_route.json").write_text(json.dumps({"route": "inline"}), encoding="utf-8")
    (run / "manifest.yaml").write_text(": not : valid : yaml :", encoding="utf-8")
    assert ims.run_gate_failures_lines(ws) == []


def test_wired_into_per_workspace_sections(tmp_path, monkeypatch):
    """The scaffold must actually be composed into the injected per-workspace context."""
    ws_root = tmp_path / "workspaces"
    _build_ws(ws_root, route="inline", outcome="partial")  # -> ws_root/site-x/...
    monkeypatch.setattr(ims, "WORKSPACES_DIR", ws_root)
    out = "\n".join(ims.per_workspace_sections("site-x"))
    assert "did not COMPLETE" in out and "self_consistency" in out
