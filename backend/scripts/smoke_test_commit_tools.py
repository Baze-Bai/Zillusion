"""Smoke test for emit-as-you-go commit tools.

Tests:
  - check_url_committed_status returns correct state
  - commit_source happy path
  - commit_source rejects: (a) URL in tree, (b) duplicate source, (c) crawled URL
  - commit_portal_tree happy path
  - commit_portal_tree auto-tombstones overlapping sources
  - remove_committed_source writes tombstone + reason required
  - Full lifecycle: commit_source → commit_portal_tree (tombstones) → check status

All tests run against a temp session directory via a stubbed RunLogger.

Usage:
  python scripts/smoke_test_commit_tools.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src.agents.agentic.tools import (  # noqa: E402
    _save_template_for_session,
    check_url_committed_status,
    commit_portal_tree,
    commit_source,
    remove_committed_source,
)
from src.services.run_logger import RunLogger, reset_run_logger, set_run_logger  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Test harness
# ──────────────────────────────────────────────────────────────────────


class _Counters:
    passes = 0
    fails = 0


def _assert(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        _Counters.passes += 1
        print(f"  ✓ {label}")
    else:
        _Counters.fails += 1
        print(f"  ✗ {label}")
        if detail:
            print(f"      {detail}")


def _unwrap(envelope: dict) -> dict:
    """Tools return {content:[{type:text, text:json_str}]}. Unwrap to dict."""
    return json.loads(envelope["content"][0]["text"])


async def _call(tool_func, **args) -> dict:
    """Call a tool's .handler with args dict."""
    return _unwrap(await tool_func.handler(args))


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


async def run_tests() -> None:
    # Test ID determines session dir name; we clean it up after
    test_qid = "smoke-test-commit-tools-1"
    session_root = Path("agent-workspace/agent-sessions") / test_qid
    if session_root.exists():
        shutil.rmtree(session_root)

    # Bind a fake RunLogger so tools.get_run_logger() returns one with
    # the right query_id. The RunLogger writes to run-logs/, we'll
    # clean that up too.
    rl = RunLogger(query_id=test_qid, query_text="smoke test")
    fake_log = rl.path
    token = set_run_logger(rl)

    try:
        await _test_check_empty()
        await _test_commit_source_happy()
        await _test_commit_source_duplicate_rejected()
        await _test_check_after_commit()
        await _test_remove_then_recommit()
        await _test_commit_portal_tree_happy()
        await _test_commit_portal_tree_tombstones()
        await _test_commit_source_in_tree_rejected()
        await _test_commit_source_crawled_rejected()
        await _test_remove_missing_url()
        await _test_remove_reason_required()
    finally:
        reset_run_logger(token)
        # Cleanup
        if session_root.exists():
            shutil.rmtree(session_root)
        if fake_log.exists():
            fake_log.unlink()


async def _test_check_empty() -> None:
    print("\n─── check_url_committed_status on empty state ───")
    r = await _call(check_url_committed_status, url="https://example.com/foo")
    _assert(r.get("recommendation", "").startswith("ok_to_commit"),
            "empty state → ok_to_commit",
            f"got: {r.get('recommendation')}")
    _assert(not r.get("found_in_committed_source"), "nothing in source yet")
    _assert(not r.get("found_in_committed_tree"), "nothing in tree yet")


async def _test_commit_source_happy() -> None:
    print("\n─── commit_source happy path ───")
    r = await _call(commit_source, source={
        "url": "https://gov.cn/dataset/abc.csv",
        "name": "Test Dataset",
        "source_type": "file",
        "description": "Test CSV dataset",
    })
    _assert(r.get("committed") is True, "commit succeeded",
            f"response: {r}")


async def _test_commit_source_duplicate_rejected() -> None:
    print("\n─── commit_source rejects duplicate ───")
    # Commit again same URL
    r = await _call(commit_source, source={
        "url": "https://gov.cn/dataset/abc.csv",
        "name": "Same dataset, different name attempt",
        "source_type": "file",
        "description": "duplicate",
    })
    _assert("error" in r, "duplicate rejected")
    _assert(r.get("duplicate_of_index") == 0, "points at original index 0",
            f"got: {r.get('duplicate_of_index')}")


async def _test_check_after_commit() -> None:
    print("\n─── check_url_committed_status after commit ───")
    r = await _call(check_url_committed_status, url="https://gov.cn/dataset/abc.csv")
    _assert(bool(r.get("found_in_committed_source")),
            "URL found in committed sources")
    _assert("already_committed_source" in r.get("recommendation", ""),
            "recommendation flags it")


async def _test_remove_then_recommit() -> None:
    print("\n─── remove_committed_source → re-commit ───")
    # First remove
    r = await _call(remove_committed_source,
                    url="https://gov.cn/dataset/abc.csv",
                    reason="testing tombstone flow")
    _assert(r.get("removed") is True, "tombstone written",
            f"response: {r}")

    # Now re-committing should succeed
    r = await _call(commit_source, source={
        "url": "https://gov.cn/dataset/abc.csv",
        "name": "Test Dataset (corrected)",
        "source_type": "file",
        "description": "re-committed after tombstone",
    })
    _assert(r.get("committed") is True,
            "re-commit succeeds after tombstone",
            f"response: {r}")


async def _test_commit_portal_tree_happy() -> None:
    print("\n─── commit_portal_tree happy path ───")
    # Pre-stage: write a template to the session cache as if crawl_list_tree had run
    csi = "test-tree-session-1"
    template = {
        "url": "https://hotels.example.com/list",
        "page_type": "list",
        "title": "Example Hotels",
        "depth": 0,
        "is_sampled": True,
        "record_count": 1234,
        "fields_available": [],
        "children": [
            {
                "url": "https://hotels.example.com/h/1",
                "page_type": "detail", "depth": 1, "is_sampled": True,
                "title": "Hotel 1", "record_count": None,
                "fields_available": [], "children": [],
            },
            {
                "url": "https://hotels.example.com/h/2",
                "page_type": "detail", "depth": 1, "is_sampled": True,
                "title": "Hotel 2", "record_count": None,
                "fields_available": [], "children": [],
            },
        ],
    }
    _save_template_for_session(csi, template)
    # Also need a fake tool_result entry in the run log so
    # _lookup_tool_result_by_session returns something. Append to the
    # RunLogger directly.
    from src.services.run_logger import log_event
    log_event("tool_result", {
        "tool": "crawl_list_tree",
        "summary": {
            "session_id": csi,
            "seed_url": template["url"],
            "page_kind_counts": {"list": 1, "leaf": 2},
            "total_pages_visited": 3,
        },
    })

    r = await _call(commit_portal_tree,
                    crawl_session_id=csi,
                    tree_summary=(
                        "Crawl visited 3 pages with page_kind_counts="
                        "{list:1, leaf:2}, found 1234 hotels via record_count."
                    ),
                    fields_available_root=["hotel_name", "price"],
                    fields_available_detail=["hotel_name", "price", "address"])
    _assert(r.get("committed") is True, "tree commit succeeded",
            f"response: {r}")
    _assert(r.get("tree_size_nodes") == 3, "3-node tree",
            f"got tree_size_nodes={r.get('tree_size_nodes')}")
    _assert(r.get("n_detail_nodes") == 2, "2 detail nodes",
            f"got n_detail_nodes={r.get('n_detail_nodes')}")


async def _test_commit_portal_tree_tombstones() -> None:
    print("\n─── commit_portal_tree auto-tombstones overlapping sources ───")
    # First commit a source whose URL is going to overlap with our tree's detail
    r = await _call(commit_source, source={
        "url": "https://shops.example.com/detail/1",
        "name": "Shop 1",
        "source_type": "embedded",
        "description": "Shop detail page",
    })
    _assert(r.get("committed") is True, "pre-tree source commit")

    # Now register a tree whose detail children include that URL
    csi = "test-tree-session-2"
    template = {
        "url": "https://shops.example.com/list",
        "page_type": "list", "title": "Shop List", "depth": 0,
        "is_sampled": True, "record_count": 99,
        "fields_available": [], "children": [
            {
                "url": "https://shops.example.com/detail/1",   # ← overlap!
                "page_type": "detail", "depth": 1, "is_sampled": True,
                "title": "Shop 1", "record_count": None,
                "fields_available": [], "children": [],
            },
        ],
    }
    _save_template_for_session(csi, template)
    from src.services.run_logger import log_event
    log_event("tool_result", {
        "tool": "crawl_list_tree",
        "summary": {
            "session_id": csi, "seed_url": template["url"],
            "page_kind_counts": {"list": 1, "leaf": 1},
            "total_pages_visited": 2,
        },
    })

    r = await _call(commit_portal_tree,
                    crawl_session_id=csi,
                    tree_summary=(
                        "Crawl visited 2 pages with page_kind_counts="
                        "{list:1, leaf:1}, the leaf was a shop detail page."
                    ),
                    fields_available_root=["shop_name"],
                    fields_available_detail=["shop_name", "address"])
    _assert(r.get("committed") is True, "tree with overlap committed")
    superseded = r.get("superseded_sources", [])
    _assert("https://shops.example.com/detail/1" in superseded,
            "overlap source URL is in superseded list",
            f"superseded: {superseded}")


async def _test_commit_source_in_tree_rejected() -> None:
    print("\n─── commit_source rejects URL already in a tree ───")
    # Try to commit the URL that's inside the tree we just committed
    r = await _call(commit_source, source={
        "url": "https://hotels.example.com/h/1",   # detail node of tree-session-1
        "name": "trying to flat-source a tree-internal URL",
        "source_type": "embedded",
        "description": "should be rejected",
    })
    _assert("error" in r, "rejected with error")
    _assert(r.get("covered_by_tree_csi") == "test-tree-session-1",
            "error cites the right tree csi",
            f"got: {r.get('covered_by_tree_csi')}")


async def _test_commit_source_crawled_rejected() -> None:
    print("\n─── commit_source rejects URL that was crawled ───")
    # The seed URL of tree-session-1 was crawled. Trying to commit it
    # as a flat source should fail with should_use=commit_portal_tree.
    r = await _call(commit_source, source={
        "url": "https://hotels.example.com/list",  # root of tree-session-1
        "name": "trying to flat-source a crawled list page",
        "source_type": "embedded",
        "description": "should be rejected",
    })
    _assert("error" in r, "rejected with error")
    # This URL is BOTH in a tree (the tree we already committed) AND was
    # crawled — the in-tree check runs first, so we expect covered_by_tree.
    # But this is also a valid "was crawled" path. Either is acceptable.
    has_anti_pattern_hint = (
        r.get("should_use") == "commit_portal_tree"
        or r.get("covered_by_tree_csi") is not None
    )
    _assert(has_anti_pattern_hint,
            "error suggests commit_portal_tree or cites covering tree",
            f"got: {r}")


async def _test_remove_missing_url() -> None:
    print("\n─── remove_committed_source on URL that wasn't committed ───")
    r = await _call(remove_committed_source,
                    url="https://nothing.example.com/never-committed",
                    reason="test missing")
    _assert("error" in r, "rejects no-such-source")


async def _test_remove_reason_required() -> None:
    print("\n─── remove_committed_source requires reason ───")
    # First commit something we can try to remove
    await _call(commit_source, source={
        "url": "https://temp.example.com/remove-test",
        "name": "for reason test",
        "source_type": "file",
        "description": "test",
    })
    # Try to remove without reason
    r = await _call(remove_committed_source,
                    url="https://temp.example.com/remove-test",
                    reason="")
    _assert("error" in r and "reason is required" in r.get("error", ""),
            "empty reason rejected",
            f"got: {r.get('error')}")


# ──────────────────────────────────────────────────────────────────────


def main() -> None:
    asyncio.run(run_tests())
    print(f"\n{'─' * 60}")
    print(f"SUMMARY: {_Counters.passes} pass / {_Counters.fails} fail")
    print(f"{'─' * 60}")
    sys.exit(0 if _Counters.fails == 0 else 1)


if __name__ == "__main__":
    main()
