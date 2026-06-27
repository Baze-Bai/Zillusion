"""Stage B — the thin SAFETY FLOOR replaces the old output-location HARD gate.

After this change the ONLY mechanically un-waveable (LLM-can't-record-pass/n-a)
conditions are: syntax parse-fail, runs_clean CRASH (non-zero exit / traceback,
NOT timeout), and a secrets shipped-leak. Everything else — including the
output-location contract — is the agent's judgment. These tests pin:
  - _persist_run_result's crash classification (the disk signal the floor reads);
  - _floor_violation fires for exactly the three floor conditions and nothing else;
  - a rerun TIMEOUT is NOT floored (correct-but-slow complex crawl);
  - update_scorecard rejects a floor pass but allows a non-floor n/a and allows
    runs_clean=pass even with a stray output_sample.json (output-location demoted).
"""

from __future__ import annotations

import json

from runtime import validator_checks as vc
from runtime import validator_tools as vt
from mcp_server.schemas import new_scorecard


# ── _persist_run_result: the on-disk crash signal ──────────────────────


def test_persist_run_result_crash_classification(tmp_path):
    cases = [
        ({"exit_code": 1, "timed_out": False, "traceback": False}, True),   # non-zero exit
        ({"exit_code": 0, "timed_out": False, "traceback": True}, True),    # traceback
        ({"exit_code": 0, "timed_out": False, "traceback": False}, False),  # clean
        ({"exit_code": None, "timed_out": True, "traceback": False}, False),  # TIMEOUT != crash
    ]
    for i, (kw, expected) in enumerate(cases):
        d = tmp_path / f"r{i}"
        d.mkdir()
        vc._persist_run_result(d, **kw)
        data = json.loads((d / "_run_result.json").read_text(encoding="utf-8"))
        assert data["crashed"] is expected, kw


# ── _floor_violation: exactly the three floors, nothing else ───────────


def test_floor_syntax(monkeypatch):
    monkeypatch.setattr(vt, "check_workflow_syntax", lambda s: {"ok": False, "errors": [{"msg": "bad"}]})
    assert vt._floor_violation("s", "val-1", "syntax") is not None
    monkeypatch.setattr(vt, "check_workflow_syntax", lambda s: {"ok": True, "errors": []})
    assert vt._floor_violation("s", "val-1", "syntax") is None


def test_floor_runs_clean_crash_but_not_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(vt, "_val_dir", lambda s, r: tmp_path)
    rerun = tmp_path / "rerun"
    rerun.mkdir()
    rerun.joinpath("_run_result.json").write_text(json.dumps({"crashed": True, "exit_code": 1}))
    assert vt._floor_violation("s", "val-1", "runs_clean") is not None
    # A timeout is NOT a crash → not floored.
    rerun.joinpath("_run_result.json").write_text(json.dumps({"crashed": False, "timed_out": True}))
    assert vt._floor_violation("s", "val-1", "runs_clean") is None
    # No rerun result yet → not floored.
    rerun.joinpath("_run_result.json").unlink()
    assert vt._floor_violation("s", "val-1", "runs_clean") is None


def test_floor_secrets(monkeypatch):
    monkeypatch.setattr(
        vt, "check_no_secret_leak",
        lambda s, r: {"na": False, "ok": False, "leaks": [{"file": "workflow.py"}]},
    )
    assert vt._floor_violation("s", "val-1", "secrets_safe") is not None
    # open API (na) → not floored
    monkeypatch.setattr(vt, "check_no_secret_leak", lambda s, r: {"na": True, "ok": True})
    assert vt._floor_violation("s", "val-1", "secrets_safe") is None
    # has creds, no leak → not floored
    monkeypatch.setattr(vt, "check_no_secret_leak", lambda s, r: {"na": False, "ok": True})
    assert vt._floor_violation("s", "val-1", "secrets_safe") is None


def test_non_floor_dims_are_never_floored(monkeypatch, tmp_path):
    # Even if every underlying check would scream, these dims have no floor —
    # they are the LLM's judgment.
    monkeypatch.setattr(vt, "_val_dir", lambda s, r: tmp_path)
    for dim in ("reproducibility", "resample_match", "field_semantics", "self_consistency",
                "endpoint_match", "completeness", "field_reasonableness"):
        assert vt._floor_violation("s", "val-1", dim) is None


# ── update_scorecard: floor rejects; everything else is the agent's call ─


def test_update_scorecard_rejects_floor_allows_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(vt, "_val_dir", lambda s, r: tmp_path)
    new_scorecard("val-1", "s").save(tmp_path / "scorecard.yaml")

    # Floor: syntax parse-fail can't be recorded pass.
    monkeypatch.setattr(vt, "check_workflow_syntax", lambda s: {"ok": False, "errors": [{"msg": "x"}]})
    rej = vt.update_scorecard("s", "val-1", "syntax", "pass")
    assert rej.get("rejected") is True and rej["attempted_status"] == "pass"

    # Non-floor: the agent rescues resample_match to n/a — allowed, no floor.
    ok = vt.update_scorecard("s", "val-1", "resample_match", "n/a", basis="RESCUED: pagination artifact")
    assert ok.get("rejected") is None and ok["status"] == "n/a"


def test_output_location_no_longer_blocks_runs_clean(monkeypatch, tmp_path):
    # Demotion: a stray output_sample.json no longer forces runs_clean=fail. With
    # a non-crash rerun result, runs_clean=pass is accepted (the agent's call).
    monkeypatch.setattr(vt, "_val_dir", lambda s, r: tmp_path)
    new_scorecard("val-1", "s").save(tmp_path / "scorecard.yaml")
    rerun = tmp_path / "rerun"
    (rerun / "sub").mkdir(parents=True)
    (rerun / "sub" / "output_sample.json").write_text("[]")  # stray (old hard-fail trigger)
    rerun.joinpath("_run_result.json").write_text(json.dumps({"crashed": False, "exit_code": 0}))
    res = vt.update_scorecard("s", "val-1", "runs_clean", "pass")
    assert res.get("rejected") is None and res["status"] == "pass"
