"""Smoke test for Fix A — DataPageNode template generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src.agents.agentic.system_prompt import SYSTEM_PROMPT  # noqa: E402
from src.agents.agentic.tools import (  # noqa: E402
    _infer_record_count,
    _infer_title,
    _to_data_page_node_template,
)


def main() -> None:
    b = SYSTEM_PROMPT.encode("utf-8")
    print(f"system_prompt: {len(b)} bytes ({len(b)/1024:.2f} KB)")
    print("imports ok\n")

    print("─── record_count inference ───")
    cases = [
        (["11269家"], "", 11269),
        (["Showing 30 of 1,775 results"], "", 1775),
        (["1111家酒店与住宿"], "", 1111),
        ([], "共找到 2849 条评论", 2849),
        ([], "约 11,264 家酒店", 11264),
        ([], "", None),
        ([], "phone 1234567890", None),  # should not false-positive on phone
    ]
    for spa, md, expected in cases:
        got = _infer_record_count(spa, md)
        marker = "✓" if got == expected else "✗"
        print(f"  {marker} spa={spa!r}, md={md[:30]!r:30} → {got!r} (expected {expected!r})")

    print("\n─── title inference ───")
    title_cases = [
        ("# Beijing Hotels\nMore text", "https://x.com/foo", "Beijing Hotels"),
        (
            "\n\n[北京贵宾楼饭店](https://x.com/h/123)\n4.5分",
            "https://x.com/h/123",
            "北京贵宾楼饭店",
        ),
        ("", "https://example.com/hotels-beijing", "hotels beijing"),
        ("![](http://img/foo.png)\n\nReal Title Here", "https://x", "Real Title Here"),
    ]
    for md, url, expected_substr in title_cases:
        got = _infer_title(md, url)
        marker = "✓" if expected_substr in got else "✗"
        print(f"  {marker} md={md[:30]!r:30} → {got!r}")

    print("\n─── full template generation ───")
    node = {
        "url": "https://example.com/list",
        "page_kind": "list",
        "depth": 0,
        "markdown_excerpt": "# Beijing Hotels\n11269家 hotels",
        "list_signals": {"spa_list_text_samples": ["11269家"]},
        "children": [
            {
                "url": "https://example.com/h/1",
                "page_kind": "leaf",
                "depth": 1,
                "markdown_excerpt": "# Hotel A",
                "list_signals": {},
                "children": [],
            },
            {
                "url": "https://example.com/page/2",
                "page_kind": "error",
                "depth": 1,
                "children": [],
            },
            {
                "url": "https://example.com/h/2",
                "page_kind": "leaf",
                "depth": 1,
                "markdown_excerpt": "# Hotel B",
                "list_signals": {},
                "children": [],
            },
            {
                "url": "https://example.com/dup",
                "page_kind": "skipped",
                "depth": 1,
                "children": [],
            },
        ],
    }
    t = _to_data_page_node_template(node)
    print(json.dumps(t, indent=2, ensure_ascii=False))

    # Assertions
    assert t["page_type"] == "list"
    assert t["record_count"] == 11269
    assert len(t["children"]) == 2, f"expected 2 children (error+skipped excluded), got {len(t['children'])}"
    assert all(c["page_type"] == "detail" for c in t["children"]), "leaf → detail mapping"
    assert all(c["is_sampled"] is True for c in t["children"])
    print("\n✅ All assertions passed")


if __name__ == "__main__":
    main()
