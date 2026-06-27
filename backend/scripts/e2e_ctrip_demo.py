"""Companion: invoke crawl_list_tree directly on a Beijing hotel list URL.

The full-graph run found data sources via search_web + fetch_page rather
than scraping commercial booking sites — a reasonable choice when the
user's intent ambiguity allows "API or dataset" interpretation. To still
demonstrate crawl_list_tree on the kind of URL it was DESIGNED for
(commercial list page with query filter), this script invokes the tool
handler directly on https://hotels.ctrip.com/hotels/list?city=1 (city=1
is Beijing's Ctrip city id).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE / ".env")
os.environ["SEARCH_FIRECRAWL_SELF_HOSTED_URL"] = "http://localhost:3002"

from src.agents.agentic.tools import crawl_list_tree  # noqa: E402
from src.services.run_logger import (  # noqa: E402
    RunLogger, log_event, reset_run_logger, set_run_logger,
)


async def main() -> None:
    seed = "https://hotels.ctrip.com/hotels/list?city=1"
    qid = "ctrip-" + uuid.uuid4().hex[:6]
    rl = RunLogger(query_id=qid, query_text=f"Direct crawl_list_tree demo: {seed}")
    token = set_run_logger(rl)
    log_event("node_start", {"node": "ctrip_demo"})

    print("=" * 70)
    print(f"Seed:        {seed}")
    print(f"Log:         {rl.path}")
    print("=" * 70)

    start = time.monotonic()
    envelope = await crawl_list_tree.handler({
        "url": seed,
        "max_depth": 2,
        "max_per_skeleton": 2,
        "max_total_pages": 10,
        "skip_pagination": True,
        "session_id": qid,
    })
    duration = time.monotonic() - start

    try:
        payload = json.loads(envelope["content"][0]["text"])
    except Exception as e:
        print(f"FAILED TO PARSE: {e}")
        reset_run_logger(token)
        return

    print(f"\nDONE in {duration:.1f}s")
    print(f"Total pages visited: {payload.get('total_pages_visited')}")
    print(f"Page kinds: {payload.get('page_kind_counts')}")
    print(f"Cache dir:  {payload.get('cache_dir')}")
    print()

    def _render(n: dict, indent: int = 0) -> None:
        prefix = "  " * indent
        url = n.get("url", "")
        kind = n.get("page_kind", "")
        chars = n.get("markdown_chars", 0)
        links_t = n.get("links_total", 0)
        links_k = n.get("links_kept", 0)
        reasons = n.get("list_signals", {}).get("reasons", [])
        stopped = (n.get("stopped_reason") or n.get("skipped_reason")
                   or n.get("error", ""))
        marker = f" [stop={stopped}]" if stopped else ""
        print(f"{prefix}[{kind}] depth={n.get('depth')} md={chars}c "
              f"links={links_t}→{links_k} reasons={reasons}{marker}")
        print(f"{prefix}     {url[:100]}")
        for c in n.get("children") or []:
            _render(c, indent + 1)

    print("Tree:")
    _render(payload.get("root") or {})

    log_event("node_complete", {
        "node": "ctrip_demo", "duration_ms": round(duration * 1000, 2),
        "n_pages": payload.get("total_pages_visited"),
    })
    log_event("run_complete", {
        "completed": True, "total_duration_ms": round(duration * 1000, 2),
    })
    reset_run_logger(token)


if __name__ == "__main__":
    asyncio.run(main())
