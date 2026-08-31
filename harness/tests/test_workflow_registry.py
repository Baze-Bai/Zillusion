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


# ── agentic route (2026-08-31) ──────────────────────────────────────────────
# Until this landed, an agentic site left NO version behind: the archiver took
# workspaces/<site>/workflow.py unconditionally and returned None when it was
# absent, which is every agentic loop. These pin the recipe generalization.


def _mk_agentic_site(root, site_id, *, brief="# Crawl brief\nHarvest every listing.\n", run=None):
    """A site whose route ended `agentic`: a crawl_brief, no workflow.py, and
    (optionally) a finished crawl under runs/<run_id>/output.json."""
    ws = root / "workspaces" / site_id
    ws.mkdir(parents=True, exist_ok=True)
    inputs = root / "inputs" / site_id
    inputs.mkdir(parents=True, exist_ok=True)
    (ws / "crawl_brief.md").write_text(brief, encoding="utf-8")
    (inputs / "goal.md").write_text("# 目标\n抓取全部房源。\n", encoding="utf-8")
    (inputs / "seed.json").write_text(
        json.dumps({"url": "https://listings.example.com"}), encoding="utf-8"
    )
    if run is not None:
        run_id, payload = run
        rd = ws / "runs" / run_id
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "output.json").write_text(payload, encoding="utf-8")
    return ws


def test_agentic_brief_is_archived(tmp_path):
    _mk_agentic_site(tmp_path, "listings-1")
    vdir = save_workflow_version("listings-1", verdict="PASS", root=tmp_path)
    assert vdir is not None, "an agentic loop must leave a version behind"
    assert (vdir / "crawl_brief.md").is_file()
    assert not (vdir / "workflow.py").exists()
    meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["recipe_kind"] == "crawl_brief"
    assert meta["recipe_file"] == "crawl_brief.md"
    assert meta["workflow_type_hint"] == "agentic"
    # No script here, so nothing may present itself as one.
    assert "workflow_sha256" not in meta


def test_agentic_doc_documents_the_resume_command(tmp_path):
    _mk_agentic_site(tmp_path, "listings-2", run=("run-abc123", '[{"a": 1}]'))
    vdir = save_workflow_version(
        "listings-2", verdict="PASS", root=tmp_path, run_id="run-abc123"
    )
    doc = (vdir / "WORKFLOW_DOC.md").read_text(encoding="utf-8")
    assert "runtime.cli crawl listings-2" in doc
    assert "--run-id run-abc123" in doc
    assert "从零重抓整站" in doc  # the omitted-run-id warning must be stated
    assert "runtime.cli run" not in doc  # the deterministic command is wrong here
    # The dispatched run's output is the agentic sample.
    assert json.loads((vdir / "output_sample.json").read_text(encoding="utf-8")) == [{"a": 1}]


def test_agentic_sample_falls_back_to_newest_run(tmp_path):
    _mk_agentic_site(tmp_path, "listings-3", run=("run-only", '[{"b": 2}]'))
    vdir = save_workflow_version("listings-3", verdict="PASS", root=tmp_path)  # no run_id
    assert json.loads((vdir / "output_sample.json").read_text(encoding="utf-8")) == [{"b": 2}]


def test_agentic_without_a_finished_run_still_archives(tmp_path):
    _mk_agentic_site(tmp_path, "listings-4")  # crawl died before finalize
    vdir = save_workflow_version("listings-4", verdict="INCONCLUSIVE", root=tmp_path)
    assert vdir is not None
    assert (vdir / "crawl_brief.md").is_file()
    assert not (vdir / "output_sample.json").exists()


def test_agentic_dedup_records_revalidation(tmp_path):
    _mk_agentic_site(tmp_path, "listings-5")
    v1 = save_workflow_version("listings-5", verdict="INCONCLUSIVE", root=tmp_path)
    v2 = save_workflow_version("listings-5", verdict="PASS", root=tmp_path)
    assert v1 == v2
    meta = json.loads((v1 / "meta.json").read_text(encoding="utf-8"))
    assert [r["verdict"] for r in meta["revalidations"]] == ["PASS"]


def test_switching_route_mints_a_new_version(tmp_path):
    """A site that moves deterministic → agentic has a different recipe even
    though the archive directory is shared; kind is part of the dedup key."""
    ws = _mk_site(tmp_path, "switcher")
    save_workflow_version("switcher", verdict="FAIL", root=tmp_path)
    (ws / "workflow.py").unlink()
    (ws / "crawl_brief.md").write_text("# Crawl brief\nToo dynamic for a script.\n", encoding="utf-8")
    v2 = save_workflow_version("switcher", verdict="PASS", root=tmp_path)
    versions = sorted((tmp_path / "workspaces" / "switcher" / "workflow_versions").iterdir())
    assert len(versions) == 2
    assert json.loads((v2 / "meta.json").read_text(encoding="utf-8"))["recipe_kind"] == "crawl_brief"


# ── deterministic sidecars ──────────────────────────────────────────────────
# The api recipe IS api_manifest.yaml; workflow.py is the shell that reads it.
# Archiving the shell alone produced a version nobody could replay.


def test_sidecars_ride_along(tmp_path):
    ws = _mk_site(tmp_path, "acme-side")
    (ws / "selectors.yaml").write_text("title: h1\n", encoding="utf-8")
    (ws / "helpers.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    (ws / "api_manifest.yaml").write_text("endpoints: []\n", encoding="utf-8")
    vdir = save_workflow_version("acme-side", verdict="PASS", root=tmp_path)
    for name in ("selectors.yaml", "helpers.py", "api_manifest.yaml"):
        assert (vdir / name).is_file(), f"{name} must be archived with the workflow"
    meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
    assert set(meta["sidecars"]) == {"selectors.yaml", "helpers.py", "api_manifest.yaml"}
    assert "download_manifest.yaml" not in meta["sidecars"]  # absent → not claimed


def test_inline_route_archives_nothing(tmp_path):
    """Inline harvested the whole set during explore: its deliverable under
    runs/ IS the artifact and there is no recipe to version."""
    ws = tmp_path / "workspaces" / "inline-site"
    (ws / "runs" / "run-x").mkdir(parents=True)
    (ws / "runs" / "run-x" / "output.json").write_text("[]", encoding="utf-8")
    assert save_workflow_version("inline-site", verdict="PASS", root=tmp_path) is None
