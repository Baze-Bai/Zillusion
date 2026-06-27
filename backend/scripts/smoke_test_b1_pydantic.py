"""Regression tests for Bug 2 (B1 false-positive on count phrases) and
Bug 3 (Pydantic int_type rejecting None for total_detail_pages).

Bug 2 — hotel-5c run: Booking tree_summary contained "$71, 4,550 hotels"
which the naive substring match read as "0 hotels" (a zero-data phrase),
generating B1_contradiction even though leaf=2 was honest.

Bug 3 — every run since hotel-5bdb42: one portal_tree got dropped at
Pydantic boundary because the agent emitted `total_detail_pages: null`
for trees where the count is genuinely unknowable.

Usage:
  python scripts/smoke_test_b1_pydantic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src.agents.agentic.runner import _zero_data_phrase_in  # noqa: E402
from src.models.page_tree import DataPageNode, DataPageTree  # noqa: E402


def test_b1_regex() -> None:
    """Bug 2: digit-prefixed phrases should NOT false-positive on count
    contexts."""
    cases = [
        # (text, expected_match_or_none, description)
        ("$71, 4,550 hotels", None, "count phrase — must NOT match (hotel-5c bug)"),
        ("1,600 records found", None, "comma-thousands record count"),
        ("we have 20 hotels available", None, "small count phrase"),
        ("found 9.0 hotels", None, "decimal point preceded by digit"),
        # Actual zero phrases — must still match
        ("returned 0 hotels in the results", "0 hotels", "literal zero count"),
        ("the page shows 0 records", "0 records", "literal zero records"),
        ("found 0 results", "0 results", "zero results"),
        # Word-only zero phrases
        ("zero hotel data extractable", "zero hotel", "word phrase"),
        ("no data in static markdown", "no data", "no-data phrase"),
        ("无数据可提取", "无数据", "Chinese zero phrase"),
        # Clean texts
        ("rich content with 11,269 hotels and 1,775 reviews", None, "rich count"),
        ("Crawl found 8 leaves and 11 list pages", None, "good narrative"),
    ]
    fails = 0
    for text, expected, desc in cases:
        got = _zero_data_phrase_in(text.lower())
        ok = got == expected
        marker = "✓" if ok else "✗"
        print(f"  {marker} {desc}")
        print(f"      input    : {text!r}")
        print(f"      expected : {expected!r}")
        print(f"      got      : {got!r}")
        if not ok:
            fails += 1
    print(f"\n  B1 regex: {len(cases) - fails}/{len(cases)} pass\n")
    assert fails == 0, f"{fails} B1 regex case(s) failed"


def test_b3_pydantic_none() -> None:
    """Bug 3: total_detail_pages: None must parse (currently being dropped)."""
    cases = [
        # (input dict, description)
        (
            {
                "root": {"url": "https://x.com/list", "page_type": "list"},
                "total_detail_pages": None,
                "sampled_detail_pages": None,
                "crawl_session_id": "test-1",
                "tree_summary": "...",
            },
            "both counts None",
        ),
        (
            {
                "root": {"url": "https://x.com/list", "page_type": "list"},
                "total_detail_pages": 137,
                "sampled_detail_pages": 5,
                "crawl_session_id": "test-2",
                "tree_summary": "...",
            },
            "both counts int (existing behavior)",
        ),
        (
            {
                "root": {"url": "https://x.com/list", "page_type": "list"},
                "total_detail_pages": None,
                "sampled_detail_pages": 0,
                "crawl_session_id": "test-3",
                "tree_summary": "...",
            },
            "mixed None + int",
        ),
    ]
    fails = 0
    for td, desc in cases:
        try:
            tree = DataPageTree.model_validate(td)
            marker = "✓"
            note = (
                f"total={tree.total_detail_pages}, "
                f"sampled={tree.sampled_detail_pages}"
            )
        except Exception as e:
            marker = "✗"
            note = f"failed: {type(e).__name__}"
            fails += 1
        print(f"  {marker} {desc} → {note}")
    print(f"\n  Pydantic None: {len(cases) - fails}/{len(cases)} pass\n")
    assert fails == 0, f"{fails} Pydantic None case(s) failed"


def main() -> None:
    print("\n─── Bug 2: B1 word-bounded regex ───\n")
    test_b1_regex()

    print("─── Bug 3: Pydantic total_detail_pages: None ───\n")
    test_b3_pydantic_none()

    print("✅ All regression tests passed")


if __name__ == "__main__":
    main()
