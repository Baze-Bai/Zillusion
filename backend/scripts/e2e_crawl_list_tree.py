"""End-to-end smoke test for the crawl_list_tree tool + RunLogger.

Runs the new crawl_list_tree against a real query-filtered HuggingFace URL
(`/datasets?search=sentiment+analysis`) so we exercise:
  - The whole `fetch_page_with_html` fallback chain (Firecrawl → Jina → httpx)
  - 5-layer prefilter on real markdown links
  - is_list_page heuristics on real content
  - Recursive crawl + samples per cluster
  - On-disk markdown cache
  - RunLogger writing every event to JSONL

Designed for manual inspection: prints log file path + cache dir + a summary
at the end, then the operator can `cat` the log file.

Usage:
  python scripts/e2e_crawl_list_tree.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Make sure src/ is importable
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src.agents.agentic.tools import crawl_list_tree  # noqa: E402
from src.services.run_logger import (  # noqa: E402
    RunLogger,
    log_event,
    reset_run_logger,
    set_run_logger,
)


async def main() -> None:
    # Seed URL — the canonical "firecrawl_map fails on this" case from briefing.
    seed = "https://huggingface.co/datasets?search=sentiment+analysis"

    rl = RunLogger(query_id="fc-docker", query_text=f"Crawl test (firecrawl-on) for {seed}")
    token = set_run_logger(rl)
    log_event("node_start", {"node": "manual_e2e_test"})

    print(f"\n{'='*70}")
    print(f"Seed URL:    {seed}")
    print(f"Log file:    {rl.path}")
    print(f"{'='*70}\n")

    start = time.monotonic()

    # Invoke the @tool-wrapped function via its .handler attribute (the
    # SdkMcpTool wrapper itself is not directly callable — that's the SDK's
    # MCP protocol contract). This is what an agent invocation would
    # effectively trigger when the model picks `crawl_list_tree` from its
    # tool set; we just bypass the LLM and call the same handler.
    try:
        envelope = await crawl_list_tree.handler({
            "url": seed,
            "max_depth": 2,
            "max_per_skeleton": 2,
            "max_total_pages": 8,
            "skip_pagination": True,
            "session_id": "fc-docker",
        })
    except Exception as e:
        log_event("error", {"scope": "e2e_test", "error": f"{type(e).__name__}: {e}"})
        print(f"CRAWL CRASHED: {e}")
        reset_run_logger(token)
        return

    duration = time.monotonic() - start

    # The MCP envelope is {"content": [{"type": "text", "text": json.dumps(payload)}]}.
    # Unpack the payload so we can show tree + summary the same way as before.
    try:
        payload = json.loads(envelope["content"][0]["text"])
    except Exception as e:
        log_event("error", {"scope": "e2e_test", "error": f"envelope_parse: {e}"})
        print(f"FAILED TO PARSE ENVELOPE: {e}")
        print(f"Raw: {envelope}")
        reset_run_logger(token)
        return

    root = payload.get("root") or {}
    kinds = payload.get("page_kind_counts", {})
    total_counter = [payload.get("total_pages_visited", 0)]

    log_event("node_complete", {
        "node": "manual_e2e_test",
        "duration_ms": round(duration * 1000, 2),
        "n_pages": total_counter[0],
        "page_kinds": kinds,
    })
    log_event("run_complete", {
        "completed": True,
        "total_duration_ms": round(duration * 1000, 2),
    })

    reset_run_logger(token)

    print(f"\n{'='*70}")
    print(f"DONE in {duration:.1f}s")
    print(f"Pages visited: {total_counter[0]}")
    print(f"Kind breakdown: {kinds}")
    print(f"Cache dir:    agent-workspace/crawl-cache/fc-docker/")
    cache = Path("agent-workspace/crawl-cache/fc-docker")
    if cache.exists():
        md_files = list(cache.glob("*.md"))
        total_kb = sum(f.stat().st_size for f in md_files) / 1024
        print(f"Markdown files: {len(md_files)}, total {total_kb:.1f} KB")
        for f in md_files:
            print(f"  {f.name}: {f.stat().st_size} bytes")
    print(f"Run log:      {rl.path}")
    print(f"Run log size: {rl.path.stat().st_size} bytes")
    print(f"{'='*70}\n")

    # Print tree skeleton for quick visual inspection
    def _render(n: dict, indent: int = 0) -> None:
        prefix = "  " * indent
        url = n.get("url", "")
        kind = n.get("page_kind", "")
        chars = n.get("markdown_chars", 0)
        links_t = n.get("links_total", 0)
        links_k = n.get("links_kept", 0)
        reasons = n.get("list_signals", {}).get("reasons", [])
        stopped = n.get("stopped_reason") or n.get("skipped_reason") or n.get("error", "")
        marker = f" [stop={stopped}]" if stopped else ""
        print(
            f"{prefix}[{kind}] depth={n.get('depth')} md={chars}c "
            f"links={links_t}→{links_k} reasons={reasons}{marker}"
        )
        print(f"{prefix}     {url[:90]}")
        for c in n.get("children") or []:
            _render(c, indent + 1)

    print("Tree:")
    _render(root)


if __name__ == "__main__":
    asyncio.run(main())
