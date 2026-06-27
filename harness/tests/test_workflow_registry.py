"""Workflow version archive (runtime/workflow_registry.py) — stdlib-only unit
coverage: first save, doc assembly (agent purpose vs goal-digest fallback),
byte-identical dedup with revalidation trace, new version on change, missing
inputs tolerated, and the no-workflow no-op."""

import json

from runtime.workflow_registry import save_workflow_version


def _mk_site(root, site_id, *, workflow="print('v1')\n", purpose=None, sample=None):
    ws = root / "workspaces" / site_id
    ws.mkdir(parents=True, exist_ok=True)
    inputs = root / "inputs" / site_id
    inputs.mkdir(parents=True, exist_ok=True)
    (ws / "workflow.py").write_text(workflow, encoding="utf-8")
    (inputs / "goal.md").write_text(
        "# 目标\n抓取 Acme 商品列表的全部记录。\n字段:title, price。\n", encoding="utf-8"
    )
    (inputs / "seed.json").write_text(
        json.dumps({"url": "https://acme.example.com/products"}), encoding="utf-8"
    )
    (ws / "task_plan.md").write_text(
        "## 目标数据与字段\n| Field | Description |\n|---|---|\n| `title` | 商品名 |\n",
        encoding="utf-8",
    )
    if purpose is not None:
        (ws / "workflow_purpose.md").write_text(purpose, encoding="utf-8")
    if sample is not None:
        (ws / "output_sample.json").write_text(sample, encoding="utf-8")
    return ws


def test_no_workflow_is_noop(tmp_path):
    (tmp_path / "workspaces" / "empty-site").mkdir(parents=True)
    assert save_workflow_version("empty-site", verdict="PASS", root=tmp_path) is None


def test_first_version_full_bundle(tmp_path):
    _mk_site(tmp_path, "acme-1", purpose="本 workflow 抓取 Acme 商品列表。",
             sample='[{"title": "Widget", "price": 9.9}]')
    vdir = save_workflow_version(
        "acme-1", verdict="PASS", exit_reason="validated", iterations_run=2,
        total_cost_usd=1.234, model="deepseek-v4-pro", root=tmp_path,
    )
    assert vdir is not None and vdir.name.startswith("v001_") and vdir.name.endswith("_PASS")
    # 四件套
    for f in ("workflow.py", "WORKFLOW_DOC.md", "meta.json", "output_sample.json"):
        assert (vdir / f).is_file(), f
    meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["version"] == 1
    assert meta["verdict"] == "PASS"
    assert meta["iterations_run"] == 2
    assert meta["model"] == "deepseek-v4-pro"
    assert meta["source_url"] == "https://acme.example.com/products"
    assert meta["workflow_type_hint"] == "extraction"
    assert len(meta["workflow_sha256"]) == 64
    doc = (vdir / "WORKFLOW_DOC.md").read_text(encoding="utf-8")
    # agent purpose 原文嵌入;goal/task_plan/运行方法/样例预览各节齐备
    assert "本 workflow 抓取 Acme 商品列表。" in doc
    assert "抓取 Acme 商品列表的全部记录" in doc          # goal
    assert "目标数据与字段" in doc                          # task_plan
    assert "runtime.cli run acme-1" in doc                  # how to run
    assert '"title": "Widget"' in doc                       # sample preview


def test_purpose_fallback_to_goal_digest(tmp_path):
    _mk_site(tmp_path, "acme-2")  # 无 workflow_purpose.md
    vdir = save_workflow_version("acme-2", verdict="INCONCLUSIVE", root=tmp_path)
    doc = (vdir / "WORKFLOW_DOC.md").read_text(encoding="utf-8")
    assert "agent 本轮未提供专门说明" in doc
    assert "抓取 Acme 商品列表的全部记录" in doc  # goal digest 兜底


def test_identical_bytes_dedup_records_revalidation(tmp_path):
    _mk_site(tmp_path, "acme-3")
    v1 = save_workflow_version("acme-3", verdict="INCONCLUSIVE", root=tmp_path)
    v_again = save_workflow_version(
        "acme-3", verdict="PASS", exit_reason="re-validated", root=tmp_path
    )
    assert v_again == v1  # 不新建目录
    versions = list((tmp_path / "workspaces" / "acme-3" / "workflow_versions").iterdir())
    assert len(versions) == 1
    meta = json.loads((v1 / "meta.json").read_text(encoding="utf-8"))
    assert len(meta["revalidations"]) == 1
    assert meta["revalidations"][0]["verdict"] == "PASS"


def test_changed_bytes_mint_new_version(tmp_path):
    ws = _mk_site(tmp_path, "acme-4")
    save_workflow_version("acme-4", verdict="FAIL", root=tmp_path)
    (ws / "workflow.py").write_text("print('v2 - fixed selector')\n", encoding="utf-8")
    v2 = save_workflow_version("acme-4", verdict="PASS", root=tmp_path)
    assert v2.name.startswith("v002_") and v2.name.endswith("_PASS")
    assert "v2 - fixed selector" in (v2 / "workflow.py").read_text(encoding="utf-8")
    versions = sorted(
        (tmp_path / "workspaces" / "acme-4" / "workflow_versions").iterdir()
    )
    assert len(versions) == 2
    assert versions[0].name.endswith("_FAIL")


def test_missing_optional_inputs_tolerated(tmp_path):
    ws = tmp_path / "workspaces" / "bare-site"
    ws.mkdir(parents=True)
    (ws / "workflow.py").write_text("print('bare')\n", encoding="utf-8")
    # 无 goal/seed/task_plan/sample/purpose — 仍能出版本与文档
    vdir = save_workflow_version("bare-site", verdict="PASS", root=tmp_path)
    assert vdir is not None
    doc = (vdir / "WORKFLOW_DOC.md").read_text(encoding="utf-8")
    assert "作用与目的" in doc and "运行方法" in doc
    meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["source_url"] is None
    assert not (vdir / "output_sample.json").exists()


def test_api_type_hint_and_credentials_note(tmp_path):
    _mk_site(tmp_path, "api-site")
    (tmp_path / "inputs" / "api-site" / "api_spec.json").write_text(
        json.dumps({"base_url": "https://api.acme.com/v1"}), encoding="utf-8"
    )
    vdir = save_workflow_version("api-site", verdict="PASS", root=tmp_path)
    meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["workflow_type_hint"] == "api"
    doc = (vdir / "WORKFLOW_DOC.md").read_text(encoding="utf-8")
    assert "credentials.json" in doc
