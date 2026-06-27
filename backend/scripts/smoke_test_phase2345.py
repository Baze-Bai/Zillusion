"""Smoke test for Phase 2/3/4 (externalize-all-external) + Phase 5 (classify_urls).

What this checks:
  1. fetch_page writes fetched/<hash>.md with markdown_chars frontmatter +
     correct _metadata.jsonl record + returns file_path/files_changed/...
  2. crawl_list_tree writes crawled/<csi>/<hash>.md with frontmatter +
     populates _node_index.jsonl + every tree node carries markdown_path
  3. classify_urls reads those persisted markdown files in parallel via
     helper LLM and returns one NodeClassification per file with the
     expected schema fields.

Skips the full LangGraph — only directly exercises the tools we changed.
Runs in ~30-60 seconds; no DataPageNode commits, no judge stage.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE / ".env")

import os  # noqa: E402
os.environ.setdefault("SEARCH_FIRECRAWL_SELF_HOSTED_URL", "http://localhost:3002")

from src.agents.agentic.tools import (  # noqa: E402
    fetch_page, crawl_list_tree, classify_urls,
)
from src.services.run_logger import RunLogger, set_run_logger, reset_run_logger  # noqa: E402


def _unwrap(envelope: dict) -> dict:
    """SDK MCP envelopes wrap payload as {content:[{type:text,text:JSON}]}."""
    return json.loads(envelope["content"][0]["text"])


def _pretty(d: dict, max_chars: int = 1200) -> str:
    s = json.dumps(d, ensure_ascii=False, indent=2, default=str)
    return s if len(s) <= max_chars else s[:max_chars] + "\n... [truncated]"


async def main() -> None:
    query_id = "smoke-" + uuid.uuid4().hex[:6]
    rl = RunLogger(query_id=query_id, query_text="smoke-phase2345")
    token = set_run_logger(rl)
    session_dir = Path("agent-workspace/agent-sessions") / query_id

    print("=" * 72)
    print(f"Smoke test query_id: {query_id}")
    print(f"Expected workspace:  {session_dir}")
    print("=" * 72)

    failures: list[str] = []

    try:
        # ── Test 1: fetch_page ──────────────────────────────────────
        print("\n[1] fetch_page(https://example.com)")
        r1 = _unwrap(await fetch_page.handler({"url": "https://example.com"}))
        print(_pretty(r1, 600))

        # Assertions
        if "file_path" not in r1:
            failures.append("fetch_page: missing file_path")
        if not r1.get("file_path", "").startswith("fetched/"):
            failures.append(f"fetch_page: file_path not under fetched/: {r1.get('file_path')!r}")
        if "files_changed" not in r1 or not r1["files_changed"]:
            failures.append("fetch_page: empty files_changed")
        if "markdown" in r1:
            failures.append("fetch_page: should NOT return inline markdown anymore")
        if r1.get("markdown_chars_total", 0) <= 0:
            failures.append("fetch_page: markdown_chars_total=0 (fetch failed?)")

        # Check the actual file
        if r1.get("file_path"):
            md_path = session_dir / r1["file_path"]
            if not md_path.exists():
                failures.append(f"fetch_page: written file not found: {md_path}")
            else:
                first_lines = md_path.read_text(encoding="utf-8").splitlines()[:6]
                has_md_chars_frontmatter = any(
                    "markdown_chars:" in line for line in first_lines
                )
                if not has_md_chars_frontmatter:
                    failures.append("fetch_page: markdown_chars NOT in frontmatter")
                print(f"    file head:")
                for line in first_lines:
                    print(f"      {line!r}")

        # Check metadata jsonl
        meta_path = session_dir / "fetched" / "_metadata.jsonl"
        if meta_path.exists():
            meta_lines = meta_path.read_text(encoding="utf-8").splitlines()
            print(f"    _metadata.jsonl has {len(meta_lines)} record(s)")
            if meta_lines:
                last = json.loads(meta_lines[-1])
                if "markdown_chars" not in last:
                    failures.append("fetch_page: _metadata.jsonl record missing markdown_chars")
        else:
            failures.append("fetch_page: _metadata.jsonl not written")

        # ── Test 2: crawl_list_tree (shallow) ───────────────────────
        print("\n[2] crawl_list_tree(https://example.com, max_total_pages=2)")
        r2 = _unwrap(await crawl_list_tree.handler({
            "url": "https://example.com",
            "max_depth": 1,
            "max_total_pages": 2,
            "session_id": "smoke-clt",
        }))
        # Truncate root for printing — could be deep
        print(_pretty({k: v for k, v in r2.items() if k != "root"}, 800))

        # Assertions
        if "cache_dir" not in r2:
            failures.append("crawl_list_tree: missing cache_dir")
        cache_dir_rel = r2.get("cache_dir", "")
        if not cache_dir_rel.startswith("crawled/"):
            failures.append(f"crawl_list_tree: cache_dir not session-relative: {cache_dir_rel!r}")

        # Check root markdown_path
        root_node = r2.get("root") or {}
        root_md_path = root_node.get("markdown_path", "")
        if not root_md_path:
            failures.append("crawl_list_tree: root.markdown_path empty")
        elif not root_md_path.startswith("crawled/"):
            failures.append(f"crawl_list_tree: root.markdown_path not relative: {root_md_path!r}")

        # Check the file
        if root_md_path:
            md = session_dir / root_md_path
            if md.exists():
                first_lines = md.read_text(encoding="utf-8").splitlines()[:6]
                has_md_chars = any("markdown_chars:" in line for line in first_lines)
                has_kind = any("page_kind:" in line for line in first_lines)
                if not has_md_chars:
                    failures.append("crawl_list_tree: markdown_chars NOT in frontmatter")
                if not has_kind:
                    failures.append("crawl_list_tree: page_kind NOT in frontmatter")
                print(f"    root file head:")
                for line in first_lines:
                    print(f"      {line!r}")
            else:
                failures.append(f"crawl_list_tree: root markdown file missing: {md}")

        # Check template node carries markdown_path
        tpl = r2.get("data_page_node_template")
        if tpl is None:
            failures.append("crawl_list_tree: data_page_node_template missing")
        elif "markdown_path" not in tpl:
            failures.append("crawl_list_tree: template node missing markdown_path field")
        else:
            print(f"    template root markdown_path = {tpl.get('markdown_path')!r}")

        # Check _node_index.jsonl
        idx_path = session_dir / "crawled" / "smoke-clt" / "_node_index.jsonl"
        if idx_path.exists():
            idx_records = [json.loads(l) for l in idx_path.read_text(encoding="utf-8").splitlines()]
            print(f"    _node_index.jsonl has {len(idx_records)} record(s)")
            if idx_records and "markdown_chars" not in idx_records[-1]:
                failures.append("crawl_list_tree: _node_index.jsonl record missing markdown_chars")
        else:
            failures.append("crawl_list_tree: _node_index.jsonl not written")

        # ── Test 3: classify_urls on the persisted markdowns ────────
        node_paths = []
        if r1.get("file_path"):
            node_paths.append(r1["file_path"])
        if root_md_path:
            node_paths.append(root_md_path)

        if not node_paths:
            print("\n[3] SKIPPED classify_urls (no paths from earlier tests)")
            failures.append("classify_urls: skipped because no input paths available")
        else:
            print(f"\n[3] classify_urls(node_paths={node_paths}, task_hint='example.com smoke test')")
            r3 = _unwrap(await classify_urls.handler({
                "node_paths": node_paths,
                "task_hint": "example.com smoke test",
            }))
            print(_pretty(r3, 1500))

            if "classifications" not in r3:
                failures.append("classify_urls: response missing 'classifications'")
            else:
                cls_list = r3["classifications"]
                if len(cls_list) != len(node_paths):
                    failures.append(
                        f"classify_urls: expected {len(node_paths)} records, got {len(cls_list)}"
                    )
                required_keys = {"file_path", "url", "classification", "secondary_types",
                                 "relevance", "confidence", "reason", "evidence_excerpt"}
                for i, c in enumerate(cls_list):
                    missing = required_keys - set(c.keys())
                    if missing:
                        failures.append(f"classify_urls[{i}]: missing keys {missing}")
                    if c["classification"] not in {"list", "embedded", "file", "api"}:
                        failures.append(
                            f"classify_urls[{i}]: invalid classification {c['classification']!r}"
                        )
                    if c["relevance"] not in {"relevant", "not_relevant"}:
                        failures.append(
                            f"classify_urls[{i}]: invalid relevance {c['relevance']!r}"
                        )

    except Exception as e:
        import traceback
        failures.append(f"EXCEPTION {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        reset_run_logger(token)

    print("\n" + "=" * 72)
    if failures:
        print(f"FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print("PASS — all assertions passed")
        print(f"Inspect artifacts under: {session_dir}")


if __name__ == "__main__":
    asyncio.run(main())
