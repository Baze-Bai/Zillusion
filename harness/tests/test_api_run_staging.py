"""Stage-5 run staging for api workflows: api_manifest.yaml + the
user-supplied credentials.json reach the isolated run workdir (including an
--output-root tree OUTSIDE the project, where no walk-up can find them)."""

from __future__ import annotations

import json

import runtime.run_exec as run_exec
import runtime.validator_checks as validator_checks


def _site(tmp_path, monkeypatch):
    monkeypatch.setattr(validator_checks, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_exec, "PROJECT_ROOT", tmp_path)
    site = "api-run-site"
    ws = tmp_path / "workspaces" / site
    ws.mkdir(parents=True)
    (ws / "workflow.py").write_text("print('hi')\n", encoding="utf-8")
    (ws / "api_manifest.yaml").write_text("workflow_type: api\n", encoding="utf-8")
    inp = tmp_path / "inputs" / site
    inp.mkdir(parents=True)
    (inp / "credentials.json").write_text(json.dumps({"api_key": "sk-x" * 8}), encoding="utf-8")
    return site


def test_stage_inputs_copies_api_manifest(tmp_path, monkeypatch):
    site = _site(tmp_path, monkeypatch)
    work = tmp_path / "workspaces" / site / "runs" / "r1"
    work.mkdir(parents=True)
    run_exec._stage_inputs(site, work)
    assert (work / "workflow.py").exists()
    assert (work / "api_manifest.yaml").exists()


def test_ensure_credentials_copies_into_work_dir(tmp_path, monkeypatch):
    site = _site(tmp_path, monkeypatch)
    work = tmp_path / "workspaces" / site / "runs" / "r1"
    work.mkdir(parents=True)
    run_exec._ensure_credentials(site, work)
    assert (work / "credentials.json").exists()


def test_ensure_credentials_outside_tree(tmp_path, tmp_path_factory, monkeypatch):
    site = _site(tmp_path, monkeypatch)
    outside = tmp_path_factory.mktemp("other-drive") / site / "runs" / "r1"
    outside.mkdir(parents=True)
    run_exec._ensure_credentials(site, outside)
    data = json.loads((outside / "credentials.json").read_text(encoding="utf-8"))
    assert data["api_key"].startswith("sk-x")


def test_ensure_credentials_noop_without_file(tmp_path, monkeypatch):
    site = _site(tmp_path, monkeypatch)
    (tmp_path / "inputs" / site / "credentials.json").unlink()
    work = tmp_path / "workspaces" / site / "runs" / "r1"
    work.mkdir(parents=True)
    run_exec._ensure_credentials(site, work)
    assert not (work / "credentials.json").exists()
