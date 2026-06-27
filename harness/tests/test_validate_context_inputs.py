"""The validator prompts must (a) point the agent at task_plan.md as Explore's
intent CONTEXT, (b) make it explicit that task_plan is a self-report — never a
basis for PASS on its own, and (c) carry the artifact-rescue contract
(distinguish a validator artifact from a real defect; mark a rescue with the
'RESCUED: ' prefix for downstream cross-examination).

These are string-contract regressions for the hybrid (LLM-final + thin floor +
adversarial cross-exam): they pin the wording the LLM relies on so a later edit
can't silently drop the guardrails.
"""

from __future__ import annotations

from runtime import validate as v


def test_all_spines_read_task_plan_as_context_not_basis():
    for build in (v._build_prompt, v._build_download_prompt, v._build_api_prompt):
        prompt = build("some-site", "val-1")
        assert "task_plan.md" in prompt
        # Self-report, never a PASS basis on its own.
        assert "NEVER a basis for PASS on its own" in prompt


def test_system_prompt_task_plan_guardrail():
    sp = v._SYSTEM_PROMPT
    assert "task_plan.md" in sp
    # task_plan informs artifact classification but cannot, alone, grant a PASS.
    assert "NEVER by itself justify a PASS" in sp


def test_system_prompt_artifact_rescue_contract():
    sp = v._SYSTEM_PROMPT
    # The defect-vs-artifact distinction and the cross-examinable rescue marker.
    assert "validator artifact" in sp
    assert "RESCUED: " in sp
    assert "re-fetchability" in sp  # the pagination/CSR limit → n/a, not fail


def test_system_prompt_exposes_run_python_zones():
    sp = v._SYSTEM_PROMPT
    # run_python is offered, with the three-zone isolation made explicit.
    assert "run_python" in sp
    assert "inputs_ro" in sp
    assert "update_scorecard" in sp  # verdicts go ONLY through it


def test_extraction_tools_swapped_resample_for_browser():
    # resample_and_compare is gone; the flexible browser + python tools are in.
    for name in ("browser_goto", "browser_content", "browser_evaluate", "run_python"):
        assert name in v._TOOL_NAMES
    assert "resample_and_compare" not in v._TOOL_NAMES


def test_system_prompt_evidence_is_cross_cutting():
    sp = v._SYSTEM_PROMPT
    # A gating dim must not hang on one tool — corroborate across tools.
    assert "Evidence is cross-cutting" in sp
    assert "LEAD, not a verdict" in sp
    assert "corroborate" in sp.lower()


def test_extraction_spine_marks_arrows_as_primary_not_sole():
    prompt = v._build_prompt("s", "val-1")
    assert "PRIMARY probe" in prompt
    assert "not the sole basis" in prompt.lower() or "not the sole" in prompt.lower()
