"""resample_match gate behaviour: n/a is non-blocking, not_verified is not.

The fixed resample_and_compare TOOL was removed (2026 — the validator now
re-fetches the live page itself via browser_goto/content/evaluate + run_python
and judges resample_match flexibly). The `resample_match` DIMENSION and its gate
semantics are unchanged, and these pin them:

  - resample_match=n/a (structurally inapplicable / re-fetchability limit, e.g.
    a single-page snapshot or a shared-listing deep-pagination crawl) + all other
    gating dims pass → overall PASS (n/a does NOT block, like api's secrets_safe);
  - resample_match=not_verified (could-have-checked-but-didn't) → overall stays
    inconclusive (not a free pass).
"""

from __future__ import annotations

from mcp_server.schemas import new_scorecard


def test_gate_passes_with_resample_na():
    sc = new_scorecard("val-1", "site-1")  # extraction catalog: 6 gating
    for dim in ("syntax", "runs_clean", "self_consistency", "reproducibility", "field_semantics"):
        sc.set_dimension(dim, "pass", basis="test")
    sc.set_dimension("resample_match", "n/a", basis="no per-record source_url (single page)")
    assert sc.overall == "pass"


def test_gate_inconclusive_with_resample_not_verified():
    # could-have-checked-but-didn't is NOT a free pass.
    sc = new_scorecard("val-2", "site-2")
    for dim in ("syntax", "runs_clean", "self_consistency", "reproducibility", "field_semantics"):
        sc.set_dimension(dim, "pass", basis="test")
    sc.set_dimension("resample_match", "not_verified", basis="transient fetch failure")
    assert sc.overall == "inconclusive"


def test_gate_passes_with_pagination_rescue_na():
    # The Ctrip outcome: resample_match rescued to n/a (shared listing source_url,
    # deep pagination — a re-fetchability limit, not a defect) + all else pass.
    sc = new_scorecard("val-3", "ctrip")
    for dim in ("syntax", "runs_clean", "self_consistency", "reproducibility", "field_semantics"):
        sc.set_dimension(dim, "pass", basis="test")
    sc.set_dimension(
        "resample_match", "n/a",
        basis="RESCUED: shared listing source_url, deep pagination; reproducibility passed",
    )
    assert sc.overall == "pass"
