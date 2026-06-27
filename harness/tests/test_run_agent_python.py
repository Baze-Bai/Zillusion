"""run_python — the validator agent's isolated, self-owned Python exec.

Three-zone isolation (the agreed model):
  - WORK (validation/<run_id>/work/): the agent's full read-write cwd; files
    persist across calls so it can build helpers.
  - EXPLORE artifacts: staged READ-ONLY as copies in work/inputs_ro/ — editing a
    copy can't touch the real artifact.
  - ADJUDICATION (zone C): scorecard.yaml / rerun/ / _run_result.json live in the
    PARENT validation/<run_id>/, are NOT in the cwd, and a relative-path script
    can't reach them (verdicts must go through update_scorecard).
"""

from __future__ import annotations

import pytest

from runtime import validator_checks as vc


@pytest.fixture
def vc_root(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _ws(root, site="s"):
    ws = root / "workspaces" / site
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def test_runs_code_and_captures_output(vc_root):
    _ws(vc_root)
    res = vc.run_agent_python("s", "val-1", "print('hi from agent'); raise SystemExit(3)")
    assert res["ran"] is True
    assert res["exit_code"] == 3 and res["timed_out"] is False
    assert "hi from agent" in res["stdout_tail"]


def test_explore_artifacts_are_read_only_copies(vc_root):
    ws = _ws(vc_root)
    (ws / "output_sample.json").write_text('[{"a": 1}]', encoding="utf-8")
    # The copy is visible under inputs_ro/ …
    res = vc.run_agent_python("s", "val-1", "import os; print(os.listdir('inputs_ro'))")
    assert "output_sample.json" in res["stdout_tail"]
    # … and clobbering the COPY never touches the real artifact.
    vc.run_agent_python("s", "val-1", "open('inputs_ro/output_sample.json','w').write('[]')")
    assert (ws / "output_sample.json").read_text(encoding="utf-8") == '[{"a": 1}]'


def test_work_is_full_rights_and_persists_across_calls(vc_root):
    _ws(vc_root)
    vc.run_agent_python("s", "val-1", "open('helper.py','w').write('X = 42\\n')")
    res = vc.run_agent_python("s", "val-1", "import helper; print(helper.X)")
    assert "42" in res["stdout_tail"]


def test_zone_c_not_in_cwd_and_untouched(vc_root):
    ws = _ws(vc_root)
    val = ws / "validation" / "val-1"
    (val / "rerun").mkdir(parents=True)
    (val / "scorecard.yaml").write_text("overall: pass\n", encoding="utf-8")
    (val / "rerun" / "_run_result.json").write_text('{"crashed": true}', encoding="utf-8")
    # cwd is work/; the adjudication files live in the PARENT, not here.
    res = vc.run_agent_python("s", "val-1", "import os; print(sorted(os.listdir('.')))")
    out = res["stdout_tail"]
    assert "scorecard.yaml" not in out
    assert "_run_result.json" not in out
    # A relative-path script genuinely can't see them, and they stay intact.
    assert (val / "scorecard.yaml").read_text(encoding="utf-8") == "overall: pass\n"


def test_timeout_is_reported_not_crashed(vc_root):
    _ws(vc_root)
    res = vc.run_agent_python("s", "val-1", "import time; time.sleep(5)", timeout_s=1)
    assert res["timed_out"] is True and res["exit_code"] is None


def test_rerun_produced_files_staged_read_only(vc_root):
    # The rerun's PRODUCED deliverables are staged read-only under inputs_ro/rerun/
    # so the agent can deeply re-examine the bytes the workflow actually produced.
    ws = _ws(vc_root)
    rerun = ws / "validation" / "val-1" / "rerun"
    (rerun / "out").mkdir(parents=True)
    (rerun / "out" / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    # control files (_*) and staged inputs are NOT produced deliverables
    (rerun / "_run_result.json").write_text('{"crashed": false}', encoding="utf-8")
    (rerun / "workflow.py").write_text("# staged input\n", encoding="utf-8")
    (rerun / "auth_state.json").write_text("{}", encoding="utf-8")

    res = vc.run_agent_python(
        "s", "val-1", "import os; print(sorted(os.listdir('inputs_ro/rerun/out')))"
    )
    assert "data.csv" in res["stdout_tail"]
    res2 = vc.run_agent_python(
        "s",
        "val-1",
        "import os; print([p for p in ('_run_result.json','workflow.py','auth_state.json') "
        "if os.path.exists('inputs_ro/rerun/'+p)])",
    )
    assert "[]" in res2["stdout_tail"]  # none of the excluded files staged

    # Clobbering the staged COPY never touches the canonical rerun file.
    vc.run_agent_python("s", "val-1", "open('inputs_ro/rerun/out/data.csv','w').write('X')")
    assert (rerun / "out" / "data.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"
