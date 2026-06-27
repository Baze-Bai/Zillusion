"""Output-location contract, both ends (2026-06-11):

  RUN side (salvage)   — run_exec._post_process materializes a misplaced
                         deliverable (e.g. output/output_sample.json) at the
                         canonical runs/<run_id>/output.json so the backend
                         still registers a download. Keeps EXISTING drifted
                         workflows deliverable.
  VALIDATOR side (gate) — run_workflow_isolated reports stray_output_samples;
                         update_scorecard mechanically refuses runs_clean
                         pass/n-a while the rerun's output_sample.json exists
                         only below a subdirectory. Keeps NEW workflows honest
                         (the fabricators-of-create precedent: the agent waved
                         a path mismatch through manually).

Deterministic — tiny throwaway workflow.py subprocesses + pure filesystem.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from runtime import run_exec
from runtime import validator_checks as vc
from runtime import validator_tools as vt

# ── throwaway workflows ──────────────────────────────────────────────

WF_DRIFT = """\
import json, pathlib
out = pathlib.Path(__file__).resolve().parent / "output"
out.mkdir(exist_ok=True)
(out / "output_sample.json").write_text(
    json.dumps([{"source_url": "https://x/1", "v": 1}]), encoding="utf-8")
print("drifted", flush=True)
"""

WF_OK = """\
import json, pathlib
pathlib.Path("output_sample.json").write_text(
    json.dumps([{"source_url": "https://x/1", "v": 1}, {"source_url": "https://x/2", "v": 2}]),
    encoding="utf-8")
print("ok", flush=True)
"""

WF_ROOT_OUTPUT_JSON = """\
import json, pathlib
pathlib.Path("output.json").write_text(
    json.dumps([{"source_url": "https://x/1"}]), encoding="utf-8")
print("root output.json", flush=True)
"""

WF_NOTHING = """\
print("no output at all", flush=True)
"""

WF_DOWNLOAD = """\
import pathlib
d = pathlib.Path("downloads"); d.mkdir(exist_ok=True)
(d / "data.csv").write_text("a,b\\n1,2\\n", encoding="utf-8")
print("downloaded", flush=True)
"""


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_site(tmp_path, monkeypatch):
    """(workflow_src, extra_files) -> (site_id, run_id); run_exec._workspace
    monkeypatched into tmp_path (mirrors test_run_kill_wake)."""
    ws_root = tmp_path / "workspaces"

    def _ws(site_id: str) -> Path:
        return ws_root / site_id

    monkeypatch.setattr(run_exec, "_workspace", _ws)

    def _make(workflow_src: str, extra: dict[str, str] | None = None) -> tuple[str, str]:
        site_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        ws = _ws(site_id)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "workflow.py").write_text(workflow_src, encoding="utf-8")
        for rel, text in (extra or {}).items():
            (ws / rel).write_text(text, encoding="utf-8")
        return site_id, run_id

    return _make


async def _run_to_done(site_id: str, run_id: str, timeout: float = 25.0) -> dict:
    res = await run_exec.start_crawl(site_id, run_id)
    assert res.get("started"), res
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        prog = run_exec.get_progress(site_id, run_id)
        if prog.get("state") in ("done", "killed", "error"):
            return prog
        await asyncio.sleep(0.2)
    raise AssertionError(f"never terminal: {run_exec.get_progress(site_id, run_id)}")


@pytest.fixture
def vt_root(tmp_path, monkeypatch):
    """Isolated PROJECT_ROOT for the scorecard gate (validator_tools)."""
    monkeypatch.setattr(vt, "PROJECT_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def vc_root(tmp_path, monkeypatch):
    """Isolated PROJECT_ROOT for validator_checks (isolated reruns)."""
    monkeypatch.setattr(vc, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _mk_rerun(
    root: Path, site: str, run: str, *, root_sample: bool = False, stray: bool = False
) -> Path:
    rerun = root / "workspaces" / site / "validation" / run / "rerun"
    rerun.mkdir(parents=True, exist_ok=True)
    if root_sample:
        (rerun / "output_sample.json").write_text("[]", encoding="utf-8")
    if stray:
        d = rerun / "output"
        d.mkdir(exist_ok=True)
        (d / "output_sample.json").write_text("[]", encoding="utf-8")
    return rerun


# ── _find_misplaced_output (pure) ────────────────────────────────────


def test_find_misplaced_empty_tree(tmp_path):
    assert run_exec._find_misplaced_output(tmp_path) is None


def test_find_misplaced_single_candidate(tmp_path):
    p = tmp_path / "output" / "output_sample.json"
    p.parent.mkdir()
    p.write_text("[]", encoding="utf-8")
    assert run_exec._find_misplaced_output(tmp_path) == p


def test_find_misplaced_contract_name_beats_depth(tmp_path):
    oj = tmp_path / "a" / "output.json"
    oj.parent.mkdir()
    oj.write_text("[]", encoding="utf-8")
    deep_sample = tmp_path / "a" / "b" / "output_sample.json"
    deep_sample.parent.mkdir(parents=True)
    deep_sample.write_text("[]", encoding="utf-8")
    # output_sample.json (contract name) wins even when deeper.
    assert run_exec._find_misplaced_output(tmp_path) == deep_sample


def test_find_misplaced_prefers_shallower_then_newer(tmp_path):
    deep = tmp_path / "a" / "b" / "output_sample.json"
    deep.parent.mkdir(parents=True)
    deep.write_text("[]", encoding="utf-8")
    shallow = tmp_path / "c" / "output_sample.json"
    shallow.parent.mkdir()
    shallow.write_text("[]", encoding="utf-8")
    assert run_exec._find_misplaced_output(tmp_path) == shallow

    # same depth → newer mtime wins
    older = tmp_path / "d" / "output_sample.json"
    older.parent.mkdir()
    older.write_text("[]", encoding="utf-8")
    now = time.time()
    os.utime(older, (now - 1000, now - 1000))
    os.utime(shallow, (now, now))
    assert run_exec._find_misplaced_output(tmp_path) == shallow


def test_find_misplaced_skips_root_and_control(tmp_path):
    (tmp_path / "output_sample.json").write_text("[]", encoding="utf-8")  # root: caller's
    hid = tmp_path / "_tmp" / "output_sample.json"
    hid.parent.mkdir()
    hid.write_text("[]", encoding="utf-8")
    tmpf = tmp_path / "x" / "output_sample.json.tmp"  # wrong name entirely
    tmpf.parent.mkdir()
    tmpf.write_text("[]", encoding="utf-8")
    assert run_exec._find_misplaced_output(tmp_path) is None


# ── _post_process via the real pump ──────────────────────────────────


async def test_drifted_output_is_salvaged(fake_site):
    site_id, run_id = fake_site(WF_DRIFT)
    prog = await _run_to_done(site_id, run_id)
    assert prog["state"] == "done" and prog["exit_code"] == 0

    run_dir = run_exec._workspace(site_id) / "runs" / run_id
    out = run_dir / "output.json"
    assert prog["output_path"] == str(out)
    assert prog["record_count"] == 1
    assert json.loads(out.read_text(encoding="utf-8")) == [{"source_url": "https://x/1", "v": 1}]
    # the stray original is preserved (run dirs are audit surface)
    assert (run_dir / "output" / "output_sample.json").is_file()


async def test_contract_workflow_unchanged(fake_site):
    site_id, run_id = fake_site(WF_OK)
    prog = await _run_to_done(site_id, run_id)
    run_dir = run_exec._workspace(site_id) / "runs" / run_id
    assert prog["output_path"] == str(run_dir / "output.json")
    assert prog["record_count"] == 2
    assert (run_dir / "output_sample.json").is_file()  # sample copy kept


async def test_root_output_json_accepted(fake_site):
    site_id, run_id = fake_site(WF_ROOT_OUTPUT_JSON)
    prog = await _run_to_done(site_id, run_id)
    run_dir = run_exec._workspace(site_id) / "runs" / run_id
    assert prog["output_path"] == str(run_dir / "output.json")
    assert prog["record_count"] == 1


async def test_no_output_stays_none(fake_site):
    site_id, run_id = fake_site(WF_NOTHING)
    prog = await _run_to_done(site_id, run_id)
    assert prog["state"] == "done"
    assert prog["output_path"] is None
    assert prog["record_count"] is None


async def test_download_type_untouched(fake_site):
    site_id, run_id = fake_site(
        WF_DOWNLOAD, extra={"download_manifest.yaml": "workflow_type: download\n"}
    )
    prog = await _run_to_done(site_id, run_id)
    run_dir = run_exec._workspace(site_id) / "runs" / run_id
    assert prog["output_path"] == str(run_dir)  # the directory, as before
    assert (run_dir / "downloads" / "data.csv").is_file()


# ── validator: stray_output_samples signal ───────────────────────────


def test_run_isolated_reports_strays(vc_root):
    site = "stray-site"
    ws = vc_root / "workspaces" / site
    ws.mkdir(parents=True)
    (ws / "workflow.py").write_text(WF_DRIFT, encoding="utf-8")
    res = vc.run_workflow_isolated(site, "val-1", timeout_s=60)
    assert res["exit_code"] == 0
    assert res["isolated_output"] is None
    assert res["stray_output_samples"] == ["output/output_sample.json"]


def test_run_isolated_no_strays_on_contract(vc_root):
    site = "ok-site"
    ws = vc_root / "workspaces" / site
    ws.mkdir(parents=True)
    (ws / "workflow.py").write_text(WF_OK, encoding="utf-8")
    res = vc.run_workflow_isolated(site, "val-1", timeout_s=60)
    assert res["isolated_output"] is not None
    assert res["stray_output_samples"] == []


# ── validator: update_scorecard hard gate ────────────────────────────


def test_stray_output_no_longer_gates_runs_clean(vt_root):
    # Stage B: output-location is ADVISORY now — a stray output_sample.json no
    # longer mechanically rejects runs_clean=pass/n-a (it's the agent's call).
    site, run = "g1", "val-1"
    vt.init_scorecard(site, run)
    _mk_rerun(vt_root, site, run, stray=True)
    for status in ("pass", "n/a"):
        res = vt.update_scorecard(site, run, "runs_clean", status)
        assert res.get("rejected") is None, res
        assert res["status"] == status
    # But the runs_clean FLOOR (a real crash) IS still un-waveable.
    rerun = vt_root / "workspaces" / site / "validation" / run / "rerun"
    rerun.joinpath("_run_result.json").write_text(
        json.dumps({"crashed": True, "exit_code": 1}), encoding="utf-8"
    )
    res = vt.update_scorecard(site, run, "runs_clean", "pass")
    assert res.get("rejected") is True and "CRASHED" in res["reason"]


def test_gate_allows_pass_with_root_sample(vt_root):
    site, run = "g2", "val-1"
    vt.init_scorecard(site, run)
    # root sample present — a stray copy elsewhere is then irrelevant
    _mk_rerun(vt_root, site, run, root_sample=True, stray=True)
    res = vt.update_scorecard(site, run, "runs_clean", "pass", basis="exit 0")
    assert res.get("rejected") is None
    assert res["status"] == "pass"


def test_gate_ignores_missing_rerun(vt_root):
    site, run = "g3", "val-1"
    vt.init_scorecard(site, run)
    res = vt.update_scorecard(site, run, "runs_clean", "pass", basis="judged without rerun")
    assert res.get("rejected") is None  # not this gate's business


def test_gate_exempts_download_catalog(vt_root):
    site, run = "g4", "val-1"
    ws = vt_root / "workspaces" / site
    ws.mkdir(parents=True)
    (ws / "download_manifest.yaml").write_text("workflow_type: download\n", encoding="utf-8")
    res = vt.init_scorecard(site, run)
    assert res["workflow_type"] == "download"
    assert "file_downloaded" in res["dimensions"]
    _mk_rerun(vt_root, site, run, stray=True)  # pathological, but must not gate
    res = vt.update_scorecard(site, run, "runs_clean", "pass", basis="exit 0")
    assert res.get("rejected") is None


def test_non_floor_dim_unaffected_by_stray(vt_root):
    # A stray output never touches a non-floor dim — the agent records it freely.
    site, run = "g5", "val-1"
    vt.init_scorecard(site, run)
    _mk_rerun(vt_root, site, run, stray=True)
    res = vt.update_scorecard(site, run, "reproducibility", "pass", basis="cold rerun matched")
    assert res.get("rejected") is None
    assert res["status"] == "pass"


def test_init_scorecard_follows_workspace_not_caller(vt_root):
    # empty workspace → extraction; a mismatched request is ignored + noted,
    # so the dimension catalog (and with it the gate) cannot be swapped away.
    res = vt.init_scorecard("g6", "val-1", workflow_type="download")
    assert res["workflow_type"] == "extraction"
    assert "file_downloaded" not in res["dimensions"]
    assert "runs_clean" in res["dimensions"]
    assert "note" in res


def test_stray_output_no_longer_gates_api(vt_root):
    # Same demotion for the api catalog: a stray no longer rejects runs_clean.
    site, run = "g7", "val-1"
    ws = vt_root / "workspaces" / site
    ws.mkdir(parents=True)
    (ws / "api_manifest.yaml").write_text("workflow_type: api\n", encoding="utf-8")
    res = vt.init_scorecard(site, run)
    assert res["workflow_type"] == "api"
    _mk_rerun(vt_root, site, run, stray=True)
    res = vt.update_scorecard(site, run, "runs_clean", "pass", basis="exit 0")
    assert res.get("rejected") is None


# ── salvage must not clobber / must fail atomically ──────────────────


WF_ROOT_PLUS_STRAY = """\
import json, pathlib
pathlib.Path("output.json").write_text(
    json.dumps([{"source_url": "https://x/1"}, {"source_url": "https://x/2"}]), encoding="utf-8")
out = pathlib.Path("output") ; out.mkdir(exist_ok=True)
(out / "output_sample.json").write_text(json.dumps([{"source_url": "https://x/1"}]), encoding="utf-8")
print("root full + stray sample", flush=True)
"""


async def test_root_output_json_not_clobbered_by_stray(fake_site):
    # A drifted workflow writing BOTH a full root output.json and a subdir
    # sample: the root deliverable must win — salvage must never overwrite it.
    site_id, run_id = fake_site(WF_ROOT_PLUS_STRAY)
    prog = await _run_to_done(site_id, run_id)
    run_dir = run_exec._workspace(site_id) / "runs" / run_id
    out = run_dir / "output.json"
    assert prog["output_path"] == str(out)
    assert prog["record_count"] == 2  # the FULL data, not the 1-record sample
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 2


def test_materialize_failure_leaves_no_partial(tmp_path, monkeypatch):
    # ENOSPC-style failure mid-copy: no half-written output.json may exist
    # under the canonical name; output_path falls back to the stray original.
    work = tmp_path / "runs" / "r1"
    stray_dir = work / "output"
    stray_dir.mkdir(parents=True)
    stray = stray_dir / "output_sample.json"
    stray.write_text(json.dumps([{"source_url": "u"}]), encoding="utf-8")

    def _boom(src, dst):
        Path(dst).write_text("{trunc", encoding="utf-8")  # simulate partial write
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(run_exec.shutil, "copy2", _boom)
    h = run_exec.RunHandle(
        site_id="s",
        run_id="r1",
        control_dir=work,
        work_dir=work,
        workflow_type="extraction",
        crawl_mode="full",
        wall_clock_cap_s=10.0,
        stall_timeout_s=10.0,
    )
    run_exec._post_process(h)
    assert h.output_path == str(stray)  # fallback to the real file
    assert not (work / "output.json").exists()  # no truncated canonical
    assert not (work / "output.json.tmp").exists()  # tmp cleaned up
    assert h.record_count == 1


def test_sample_directory_not_adopted(tmp_path):
    # output_sample.json as a DIRECTORY at the root is not a deliverable;
    # a real stray below still gets salvaged.
    work = tmp_path / "runs" / "r2"
    (work / "output_sample.json").mkdir(parents=True)  # directory, same name
    stray = work / "data" / "output_sample.json"
    stray.parent.mkdir()
    stray.write_text(json.dumps([{"source_url": "u"}]), encoding="utf-8")
    h = run_exec.RunHandle(
        site_id="s",
        run_id="r2",
        control_dir=work,
        work_dir=work,
        workflow_type="extraction",
        crawl_mode="full",
        wall_clock_cap_s=10.0,
        stall_timeout_s=10.0,
    )
    run_exec._post_process(h)
    assert h.output_path == str(work / "output.json")
    assert h.record_count == 1


# ── verdict reconciliation (the line is a claim; the disk is the truth) ──


from mcp_server.schemas import new_scorecard  # noqa: E402
from runtime.validate import ValidationSummary, _reconcile_verdict  # noqa: E402

_EXTRACTION_GATING = (
    "syntax",
    "runs_clean",
    "self_consistency",
    "reproducibility",
    "resample_match",
    "field_semantics",
)


def _save_scorecard(root: Path, site: str, run: str, statuses: dict[str, str]) -> None:
    sc = new_scorecard(run, site)
    for dim, status in statuses.items():
        sc.set_dimension(dim, status)
    p = root / "workspaces" / site / "validation" / run / "scorecard.yaml"
    sc.save(p)


def _summary(
    verdict: str, *, site: str = "rsite", workflow_type: str = "extraction"
) -> ValidationSummary:
    s = ValidationSummary()
    s.site_id = site
    s.workflow_type = workflow_type
    s.verdict = verdict
    s.run_id = "model-claimed-id"  # must get pinned back
    return s


def test_reconcile_pass_without_scorecard_downgrades(tmp_path):
    s = _summary("PASS")
    _reconcile_verdict(s, tmp_path, "val-r1")
    assert s.verdict == "INCONCLUSIVE"
    assert s.run_id == "val-r1"  # pinned to the assigned id
    assert "missing" in (s.reason or "")


def test_reconcile_pass_clamped_to_scorecard_fail(tmp_path):
    _save_scorecard(tmp_path, "rsite", "val-r2", {"syntax": "fail"})
    s = _summary("PASS")
    _reconcile_verdict(s, tmp_path, "val-r2")
    assert s.verdict == "FAIL"


def test_reconcile_pass_clamped_to_inconclusive(tmp_path):
    # one gating dim left not_verified → overall inconclusive
    _save_scorecard(tmp_path, "rsite", "val-r3", {d: "pass" for d in _EXTRACTION_GATING[:-1]})
    s = _summary("PASS")
    _reconcile_verdict(s, tmp_path, "val-r3")
    assert s.verdict == "INCONCLUSIVE"


def test_reconcile_no_longer_revokes_on_stray_rerun(tmp_path):
    # Stage B: output-location is advisory — reconcile no longer revokes a PASS
    # whose rerun left a stray output_sample.json. The floor is enforced at write
    # time by update_scorecard, not re-litigated at consumption.
    _save_scorecard(tmp_path, "rsite", "val-r4", {d: "pass" for d in _EXTRACTION_GATING})
    rerun = tmp_path / "workspaces" / "rsite" / "validation" / "val-r4" / "rerun" / "output"
    rerun.mkdir(parents=True)
    (rerun / "output_sample.json").write_text("[]", encoding="utf-8")
    s = _summary("PASS")
    _reconcile_verdict(s, tmp_path, "val-r4")
    assert s.verdict == "PASS"


def test_reconcile_clean_pass_stays_pass(tmp_path):
    _save_scorecard(tmp_path, "rsite", "val-r5", {d: "pass" for d in _EXTRACTION_GATING})
    rerun = tmp_path / "workspaces" / "rsite" / "validation" / "val-r5" / "rerun"
    rerun.mkdir(parents=True)
    (rerun / "output_sample.json").write_text("[]", encoding="utf-8")
    s = _summary("PASS")
    _reconcile_verdict(s, tmp_path, "val-r5")
    assert s.verdict == "PASS"


def test_reconcile_never_upgrades(tmp_path):
    _save_scorecard(tmp_path, "rsite", "val-r6", {d: "pass" for d in _EXTRACTION_GATING})
    s = _summary("FAIL")
    _reconcile_verdict(s, tmp_path, "val-r6")
    assert s.verdict == "FAIL"  # conservative claims stand even over a green scorecard


def test_reconcile_download_exempt_from_stray_check(tmp_path):
    # download catalog: all gating dims pass → overall pass; a stray sample in
    # rerun/ (pathological) must not revoke the PASS for a download workflow.
    sc = new_scorecard("val-r7", "rsite", "download")
    for dim, d in sc.dimensions.items():
        if d.gating:
            sc.set_dimension(dim, "pass")
    p = tmp_path / "workspaces" / "rsite" / "validation" / "val-r7" / "scorecard.yaml"
    sc.save(p)
    rerun = tmp_path / "workspaces" / "rsite" / "validation" / "val-r7" / "rerun" / "output"
    rerun.mkdir(parents=True)
    (rerun / "output_sample.json").write_text("[]", encoding="utf-8")
    s = _summary("PASS", workflow_type="download")
    _reconcile_verdict(s, tmp_path, "val-r7")
    assert s.verdict == "PASS"
