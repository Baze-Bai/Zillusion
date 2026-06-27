"""Stage-2 loop routing: the explore-loop's route-aware exit contract.

Deterministic (no LLM). Pins:
  - _read_crawl_route: absent / corrupt / unknown-route files all read as "no
    usable route" (the loop iterates with a notice);
  - _check_inline_artifact: the inline route's deliverable spot-check (real
    artifacts built through crawl_emit, not mocks);
  - _crawl_outcome_to_verdict: dispatched-crawl outcome → loop verdict mapping;
  - declare_crawl_route's merge rule: a same-route re-declare preserves extra
    keys (inline's run_id from commit_inline_dataset); a route CHANGE drops them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from runtime import crawl_emit as ce
from runtime import run_tools as rt
from runtime import explore_loop as xl


@pytest.fixture
def fake_ws(tmp_path, monkeypatch):
    """Point crawl_emit / run_tools / explore_loop at one throwaway
    workspaces/ root; returns a site-id factory."""
    ws_root = tmp_path / "workspaces"

    def _ws(site_id: str) -> Path:
        return ws_root / site_id

    monkeypatch.setattr(ce, "_workspace", _ws)
    monkeypatch.setattr(rt, "_workspace", _ws)
    monkeypatch.setattr(xl, "_workspace_dir", _ws)
    ce._CRAWL_STATES.clear()

    def _make() -> str:
        site = f"site-{uuid.uuid4().hex[:6]}"
        _ws(site).mkdir(parents=True, exist_ok=True)
        return site

    return _make


# ── _read_crawl_route ────────────────────────────────────────────────


def test_read_route_absent_is_none(fake_ws):
    assert xl._read_crawl_route(fake_ws()) is None


def test_read_route_valid(fake_ws):
    site = fake_ws()
    (xl._workspace_dir(site) / "crawl_route.json").write_text(
        json.dumps({"route": "agentic", "rationale": "dynamic templates"}), encoding="utf-8"
    )
    info = xl._read_crawl_route(site)
    assert info and info["route"] == "agentic"


def test_read_route_corrupt_or_unknown_is_none(fake_ws):
    site = fake_ws()
    p = xl._workspace_dir(site) / "crawl_route.json"
    p.write_text("{not json", encoding="utf-8")
    assert xl._read_crawl_route(site) is None
    p.write_text(json.dumps({"route": "warp-drive"}), encoding="utf-8")
    assert xl._read_crawl_route(site) is None


# ── _check_inline_artifact ───────────────────────────────────────────


def test_inline_check_requires_run_id(fake_ws):
    ok, detail = xl._check_inline_artifact(fake_ws(), None)
    assert not ok and "run_id" in detail


def test_inline_check_passes_on_real_artifact(fake_ws):
    site = fake_ws()
    run_id = "inline-test1"
    ce.init_crawl(
        site, run_id, identifier_field="id", anchor={"hardness": "hard", "estimated_total": 2}
    )
    ce.commit_records(site, run_id, [{"id": 1, "source_url": "u1"}, {"id": 2, "source_url": "u2"}])
    ce.finalize_crawl(site, run_id, "pass", "full set committed inline")
    ok, detail = xl._check_inline_artifact(site, run_id)
    assert ok and "2 records" in detail


def test_inline_check_fails_on_incomplete_outcome(fake_ws):
    site = fake_ws()
    run_id = "inline-test2"
    ce.init_crawl(site, run_id, anchor={"hardness": "hard", "estimated_total": 100})
    ce.commit_records(site, run_id, [{"id": 1, "source_url": "u1"}])
    # completeness fail → gate computes partial, not complete
    ce.finalize_crawl(site, run_id, "fail", "only 1/100 reachable")
    ok, detail = xl._check_inline_artifact(site, run_id)
    assert not ok and "outcome=partial" in detail


def test_inline_check_fails_on_missing_manifest(fake_ws):
    ok, detail = xl._check_inline_artifact(fake_ws(), "inline-ghost")
    assert not ok and "manifest" in detail.lower()


# ── _crawl_outcome_to_verdict ────────────────────────────────────────


def test_crawl_outcome_mapping():
    assert xl._crawl_outcome_to_verdict("COMPLETE") == "PASS"
    for o in ("PARTIAL", "FAILED", "ABORTED"):
        assert xl._crawl_outcome_to_verdict(o) == "FAIL"
    for o in ("ERROR", "UNSET", None, ""):
        assert xl._crawl_outcome_to_verdict(o) == "ERROR"


# ── declare_crawl_route merge rule (server tool) ─────────────────────


async def test_declare_same_route_preserves_run_id(tmp_path, monkeypatch):
    import mcp_server.server as srv

    monkeypatch.setattr(srv, "WORKSPACES_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(srv, "_active_site", "t-merge")
    route_path = tmp_path / "workspaces" / "t-merge" / "crawl_route.json"
    route_path.parent.mkdir(parents=True, exist_ok=True)
    # commit_inline_dataset writes route=inline WITH run_id
    route_path.write_text(
        json.dumps({"route": "inline", "rationale": "small", "run_id": "inline-abc"}),
        encoding="utf-8",
    )

    # Same-route re-declare → run_id preserved
    res = await srv.declare_crawl_route("inline", "tiny full set, harvested in-session")
    assert res["status"] == "ok"
    data = json.loads(route_path.read_text(encoding="utf-8"))
    assert data["run_id"] == "inline-abc"
    assert data["rationale"] == "tiny full set, harvested in-session"

    # Route CHANGE → stale extras dropped
    res2 = await srv.declare_crawl_route("deterministic", "actually workflow-able")
    assert res2["status"] == "ok"
    data2 = json.loads(route_path.read_text(encoding="utf-8"))
    assert data2["route"] == "deterministic" and "run_id" not in data2


async def test_declare_rejects_unknown_route(tmp_path, monkeypatch):
    import mcp_server.server as srv

    monkeypatch.setattr(srv, "WORKSPACES_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(srv, "_active_site", "t-bad")
    res = await srv.declare_crawl_route("warp-drive", "nope")
    assert res["status"] == "error"


# ── IterRecord carries route fields ──────────────────────────────────


def test_iter_record_serializes_route_fields():
    rec = xl.IterRecord(iter_n=1, started_at="t")
    rec.route = "agentic"
    rec.crawl_run_id = "run-x"
    rec.crawl_outcome = "COMPLETE"
    d = rec.to_dict()
    assert (
        d["route"] == "agentic"
        and d["crawl_run_id"] == "run-x"
        and d["crawl_outcome"] == "COMPLETE"
    )
