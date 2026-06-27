"""H9: finalize_crawl's hard-anchor completeness backstop.

A HARD anchor with a concrete estimated_total makes "pass" mechanically
checkable — a self-attested pass that did not cover the set is downgraded to
not_verified (so the run gates to PARTIAL, never a false COMPLETE)."""

from __future__ import annotations

import shutil
import uuid

import pytest
import yaml

from runtime import crawl_emit
from runtime.run_tools import _run_dir


def _site():
    return f"_t_anchor_{uuid.uuid4().hex[:8]}"


def _completeness(site, run):
    man = yaml.safe_load((_run_dir(site, run) / "manifest.yaml").read_text(encoding="utf-8"))
    return man["dimensions"]["completeness"]


def _hard(est):
    return {
        "hardness": "hard",
        "estimated_total": est,
        "full_set": f"{est} items",
        "enumeration": "paginate",
        "termination": "cursor end",
    }


def _records(n):
    # source_url required by the commit_records floor (the dedup key is `id`).
    return [{"id": i, "v": f"r{i}", "source_url": f"https://ex/{i}"} for i in range(n)]


@pytest.fixture
def site():
    s = _site()
    yield s
    shutil.rmtree(_run_dir(s, "x").parent.parent, ignore_errors=True)


def test_hard_anchor_shortfall_downgrades_pass(site):
    run = "r1"
    crawl_emit.init_crawl(site, run, identifier_field="id", anchor=_hard(1000))
    crawl_emit.commit_records(site, run, _records(50))  # 50/1000 — gross shortfall
    out = crawl_emit.finalize_crawl(site, run, "pass", "agent says done")
    c = _completeness(site, run)
    assert c["status"] == "not_verified", c
    assert "DOWNGRADED" in c["basis"]
    assert out["outcome"] in ("partial", "aborted")  # never COMPLETE


def test_hard_anchor_met_keeps_pass(site):
    run = "r2"
    crawl_emit.init_crawl(site, run, identifier_field="id", anchor=_hard(50))
    crawl_emit.commit_records(site, run, _records(50))
    crawl_emit.finalize_crawl(site, run, "pass", "covered the full set")
    assert _completeness(site, run)["status"] == "pass"


def test_hard_anchor_tolerance_allows_small_shortfall(site):
    run = "r3"
    crawl_emit.init_crawl(site, run, identifier_field="id", anchor=_hard(100))
    crawl_emit.commit_records(site, run, _records(99))  # 99/100 = 99% ≥ 98% tol
    crawl_emit.finalize_crawl(site, run, "pass", "all but one")
    assert _completeness(site, run)["status"] == "pass"


def test_tombstones_count_toward_processed(site):
    run = "r4"
    crawl_emit.init_crawl(site, run, identifier_field="id", anchor=_hard(100))
    crawl_emit.commit_records(site, run, _records(60))
    for i in range(40):
        crawl_emit.record_skip(site, run, f"u{i}", "404")  # 60 + 40 = 100 processed
    crawl_emit.finalize_crawl(site, run, "pass", "60 scraped + 40 skipped = full set")
    assert _completeness(site, run)["status"] == "pass"


def test_soft_anchor_stays_agent_judged(site):
    run = "r5"
    soft = {
        "hardness": "soft",
        "full_set": "infinite scroll",
        "enumeration": "scroll",
        "termination": "3 empty",
    }
    crawl_emit.init_crawl(site, run, identifier_field="id", anchor=soft)
    crawl_emit.commit_records(site, run, _records(7))
    crawl_emit.finalize_crawl(site, run, "pass", "3 consecutive empty scroll batches")
    assert _completeness(site, run)["status"] == "pass"  # no estimated_total → no backstop


def test_pass_with_empty_basis_downgraded(site):
    run = "r6"
    crawl_emit.init_crawl(site, run, identifier_field="id", anchor=_hard(10))
    crawl_emit.commit_records(site, run, _records(10))
    crawl_emit.finalize_crawl(site, run, "pass", "")  # met the anchor but no justification
    c = _completeness(site, run)
    assert c["status"] == "not_verified" and "no completeness basis" in c["basis"]
