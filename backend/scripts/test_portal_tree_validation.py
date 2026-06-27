"""Dry-run portal_tree validation against the historic hotel-5bdb42 run log.

Reconstructs the 5 portal_trees the agent emitted in that run (Ctrip,
Booking, Dianping, Agoda, Trip.com) and runs them through
_validate_portal_tree_evidence using that run's JSONL log as the evidence
source.

Expected outcomes:
  - ctrip      → 0 warnings (called crawl_list_tree, narrative reasonable)
  - booking    → B1_contradiction (8 leaves vs "zero hotel data")
  - dianping   → 0 warnings (small but accurate)
  - agoda      → E1_no_call (no crawl_list_tree call existed)
  - tripcom    → E1_no_call (no crawl_list_tree call existed)

Usage:
  python scripts/test_portal_tree_validation.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

os.environ.setdefault("SEARCH_FIRECRAWL_USE_SELF_HOSTED", "true")
os.environ["SEARCH_FIRECRAWL_SELF_HOSTED_URL"] = "http://localhost:3002"

from src.agents.agentic.runner import (  # noqa: E402
    _load_portal_tree_evidence,
    _validate_portal_tree_evidence,
)
from src.models.page_tree import DataPageNode, DataPageTree  # noqa: E402
from src.services.run_logger import (  # noqa: E402
    RunLogger,
    reset_run_logger,
    set_run_logger,
)


HISTORIC_LOG = HERE / "agent-workspace" / "run-logs" / "20260514T212242-hotel-5b.log"


# Reconstruction of the 5 portal_trees from hotel-5bdb42. tree_summary text
# is the narrative the agent actually emitted (per the agent_text events in
# that run / our earlier review). Each tree has NO crawl_session_id since
# the historic schema didn't have that field — perfect for testing the
# warn-only fallback behavior.
HISTORIC_TREES: list[tuple[str, DataPageTree]] = [
    (
        "ctrip",
        DataPageTree(
            root=DataPageNode(
                url="https://m.ctrip.com/webapp/hotel/beijing1",
                page_type="list",
                fields_available=["hotel_name", "star_rating", "review_score"],
                record_count=None,
                children=[
                    DataPageNode(url="https://m.ctrip.com/html5/hotel/hoteldetail/429158.html",
                                 page_type="detail", depth=1),
                    DataPageNode(url="https://m.ctrip.com/html5/hotel/hoteldetail/12345.html",
                                 page_type="detail", depth=1),
                ],
            ),
            tree_summary=(
                "SPA — hotel cards JS-rendered, static markdown shows first 2-3 "
                "hotel cards with names/stars/ratings. crawl_list_tree visited "
                "3 pages (root list + 2 detail pages). Detail pages show room "
                "types, facilities, reviews."
            ),
            crawl_session_id="",  # Historic run pre-dates the field
        ),
    ),
    (
        "booking",
        DataPageTree(
            root=DataPageNode(
                url="https://www.booking.com/city/cn/beijing.zh-cn.html",
                page_type="list",
                record_count=1111,
                fields_available=["hotel_name", "star_rating", "price"],
            ),
            tree_summary=(
                "SPA — hotel cards fully JS-rendered, static markdown (76KB) "
                "primarily nav/chrome/filters. Search snippet indicates "
                "'1111家酒店与住宿供选择'. crawl_list_tree was executed but "
                "output exceeded display limits; page_kind_counts expected to "
                "show mostly list with few extractable detail links. Zero "
                "hotel data in static markdown."
            ),
            crawl_session_id="",
        ),
    ),
    (
        "dianping",
        DataPageTree(
            root=DataPageNode(
                url="https://m.dianping.com/awp/h5/hotel-dp/list/list.html?cityid=1&regionid=9&starid=171",
                page_type="list",
            ),
            tree_summary=(
                "SPA — hotel cards JS-rendered. crawl_list_tree visited 1 "
                "page; static markdown (1.6KB) only shows date picker + filter "
                "UI; result area shows '没有找到合适的商户喔'."
            ),
            crawl_session_id="",
        ),
    ),
    (
        "agoda",
        DataPageTree(
            root=DataPageNode(
                url="https://www.agoda.com/zh-cn/city/beijing-cn.html",
                page_type="list",
            ),
            tree_summary=(
                "SPA — full cookie wall / JS gate. fetch_page returned only "
                "325 chars of cookie consent UI. Zero hotel data in static "
                "markdown. Requires Playwright with cookie acceptance + "
                "scroll to render hotel cards."
            ),
            crawl_session_id="",
        ),
    ),
    (
        "tripcom",
        DataPageTree(
            root=DataPageNode(
                url="https://www.trip.com/hotels/list?city=1",
                page_type="list",
            ),
            tree_summary=(
                "SPA — hotel cards fully JS-rendered. fetch_page returned "
                "4.3KB of nav/chrome only. Zero hotel data in static markdown."
            ),
            crawl_session_id="",
        ),
    ),
]


def main() -> None:
    if not HISTORIC_LOG.exists():
        print(f"Historic log not found: {HISTORIC_LOG}")
        sys.exit(1)

    # Bind a RunLogger that points at the historic file. We override the
    # `.path` attribute after construction — RunLogger.__init__ writes a
    # `run_start` record to a fresh path, but we want validation to READ from
    # the historic JSONL. So construct a throwaway logger, then point .path
    # at the real log.
    fake_rl = RunLogger(query_id="dryrun-hotel-5b", query_text="dry-run for validation test")
    fake_rl.path = HISTORIC_LOG
    token = set_run_logger(fake_rl)
    try:
        by_session, by_url = _load_portal_tree_evidence()
    finally:
        reset_run_logger(token)

    print(f"\nHistoric run log: {HISTORIC_LOG.name}")
    print(f"Loaded evidence: {len(by_session)} session-indexed, "
          f"{len(by_url)} URL-indexed tool_result entries")
    print(f"  by_session keys: {list(by_session.keys())}")
    print(f"  by_url keys:")
    for u in by_url:
        print(f"    {u}")

    print(f"\n{'─' * 70}")
    print(f"Per-tree validation results")
    print(f"{'─' * 70}\n")

    pass_count = 0
    fail_count = 0
    # All 5 historic trees lack crawl_session_id (the field didn't exist
    # then), so every tree gets E1_csi_missing_url_match — the warn-only
    # fallback that says "tool was called, agent forgot to anchor".
    # What DIFFERENTIATES the trees is the OTHER codes layered on top:
    #   - Booking adds B1_contradiction (8 leaves vs "zero hotel data")
    #   - Agoda / Trip.com get E1_no_call ONLY (no tool call recorded at
    #     all — these never trigger the csi_missing_url_match fallback
    #     because the URL is also absent from by_url)
    expectations = {
        "ctrip":    ["E1_csi_missing_url_match"],
        "booking":  ["E1_csi_missing_url_match", "B1_contradiction"],
        "dianping": ["E1_csi_missing_url_match"],
        "agoda":    ["E1_no_call"],
        "tripcom":  ["E1_no_call"],
    }

    for label, tree in HISTORIC_TREES:
        warnings = _validate_portal_tree_evidence(tree, by_session, by_url)
        codes = [w.split(":", 1)[0] for w in warnings]
        expected = expectations[label]

        # Exact match — the set of warning codes must equal the expected set.
        # (Sorted comparison so order doesn't matter.)
        ok = sorted(codes) == sorted(expected)

        marker = "✓ PASS" if ok else "✗ FAIL"
        print(f"{marker}  [{label}]  expected={expected or 'no warnings'}")
        print(f"         actual codes: {codes or '(none)'}")
        for w in warnings:
            print(f"         - {w}")
        print()

        if ok:
            pass_count += 1
        else:
            fail_count += 1

    print(f"{'─' * 70}")
    print(f"SUMMARY: {pass_count} passed, {fail_count} failed")
    print(f"{'─' * 70}\n")
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
