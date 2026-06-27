"""Batch test of crawl_list_tree against the 5 OTA hotel sites that came up
sparse in the 2026-05-14 hotel run (hotel-5bdb42).

For each site we call crawl_list_tree once with the SAME parameters the agent
used (max_depth=2, max_per_skeleton=2, max_total_pages=20), then collect:
  - tree shape (root.page_kind, children count, recursion depth reached)
  - fetch outcome (firecrawl success / partial / failed, html+md sizes)
  - list_signals.reasons (which heuristic flagged it as list)
  - links_total / links_kept (after 5-layer prefilter)
  - markdown_excerpt first 500 chars (to diagnose "what came back")

This is a *baseline* — same defaults as the agent. A follow-up could re-run
with `wait_for=5000` to test the hydration-time hypothesis.

Usage:
  python scripts/test_hotel_sites_crawl.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Host-side execution needs localhost, not the Docker-internal `firecrawl`
# hostname that's the default in src/config.py. Match what e2e_full_graph.py
# and e2e_ctrip_demo.py do.
os.environ.setdefault("SEARCH_FIRECRAWL_USE_SELF_HOSTED", "true")
os.environ["SEARCH_FIRECRAWL_SELF_HOSTED_URL"] = "http://localhost:3002"

# Python-side tee to a UTF-8 file. PowerShell 5.1's Tee-Object writes UTF-16
# without an -Encoding flag, which then trips up grep ("Binary file matches")
# when streaming progress via Monitor.
_LOG_FILE = HERE / "agent-workspace" / "hotel-baseline-progress.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_fh = _LOG_FILE.open("w", encoding="utf-8", buffering=1)  # line-buffered
_orig_print = print

def print(*args, **kwargs):  # noqa: A001
    _orig_print(*args, **kwargs)
    msg = " ".join(str(a) for a in args)
    _log_fh.write(msg + "\n")
    _log_fh.flush()

from src.agents.agentic.tools import crawl_list_tree  # noqa: E402
from src.services.run_logger import (  # noqa: E402
    RunLogger,
    log_event,
    reset_run_logger,
    set_run_logger,
)


SITES = [
    ("ctrip",     "https://m.ctrip.com/webapp/hotel/beijing1"),
    ("booking",   "https://www.booking.com/city/cn/beijing.zh-cn.html"),
    ("dianping",  "https://m.dianping.com/awp/h5/hotel-dp/list/list.html?cityid=1&regionid=9&starid=171"),
    ("agoda",     "https://www.agoda.com/zh-cn/city/beijing-cn.html"),
    ("tripcom",   "https://www.trip.com/hotels/list?city=1"),
]

PARAMS = {
    "max_depth": 2,
    "max_per_skeleton": 2,
    "max_total_pages": 20,
    "skip_pagination": True,
}


def _summarize_root(root: dict) -> dict:
    """Pull just the fields we want per-site for comparison."""
    list_sig = root.get("list_signals", {}) or {}
    children = root.get("children") or []
    return {
        "page_kind": root.get("page_kind"),
        "is_list": list_sig.get("is_list"),
        "list_reasons": list_sig.get("reasons", []),
        "markdown_chars": root.get("markdown_chars", 0),
        "links_total": root.get("links_total", 0),
        "links_kept": root.get("links_kept", 0),
        "n_clusters": list_sig.get("n_clusters", 0),
        "top_cluster_share": list_sig.get("top_cluster_share", 0),
        "spa_list_text_samples": list_sig.get("spa_list_text_samples", []),
        "children_count": len(children),
        "child_kinds": [c.get("page_kind") for c in children],
        "stopped_reason": root.get("stopped_reason"),
        "error": root.get("error"),
        "markdown_excerpt": (root.get("markdown_excerpt") or "")[:500],
    }


async def test_one(label: str, url: str) -> dict:
    print(f"\n{'─'*70}")
    print(f"[{label}] {url}")
    print(f"{'─'*70}")

    start = time.monotonic()
    try:
        envelope = await crawl_list_tree.handler({
            "url": url,
            "session_id": f"baseline-test-{label}",
            **PARAMS,
        })
        payload = json.loads(envelope["content"][0]["text"])
    except Exception as e:
        elapsed = time.monotonic() - start
        print(f"  CRASHED after {elapsed:.1f}s: {type(e).__name__}: {e}")
        return {
            "label": label, "url": url, "crashed": True,
            "error": f"{type(e).__name__}: {e}", "elapsed_s": elapsed,
        }
    elapsed = time.monotonic() - start

    if not payload.get("ok", True):
        print(f"  ERROR after {elapsed:.1f}s: {payload.get('error')}")
        return {
            "label": label, "url": url, "elapsed_s": elapsed,
            "tool_error": payload.get("error"), "payload": payload,
        }

    # crawl_list_tree returns {"ok": True, "data": {...}} via _ok wrapper.
    data = payload.get("data") or payload
    root = data.get("root") or {}
    summary = _summarize_root(root)

    result = {
        "label": label,
        "url": url,
        "elapsed_s": round(elapsed, 1),
        "total_pages_visited": data.get("total_pages_visited"),
        "page_kind_counts": data.get("page_kind_counts"),
        "root_summary": summary,
    }

    print(f"  elapsed:     {elapsed:.1f}s")
    print(f"  pages:       {data.get('total_pages_visited')}")
    print(f"  kinds:       {data.get('page_kind_counts')}")
    print(f"  root.kind:   {summary['page_kind']}")
    print(f"  is_list:     {summary['is_list']} (reasons: {summary['list_reasons']})")
    print(f"  markdown:    {summary['markdown_chars']:,} chars")
    print(f"  links:       {summary['links_total']} → {summary['links_kept']} kept")
    print(f"  children:    {summary['children_count']} ({summary['child_kinds']})")
    if summary["spa_list_text_samples"]:
        print(f"  spa hits:    {summary['spa_list_text_samples'][:3]}")
    if summary["error"]:
        print(f"  error:       {summary['error']}")
    return result


async def main() -> None:
    rl = RunLogger(query_id="hotel-baseline", query_text="Hotel sites baseline crawl test (5 OTAs)")
    token = set_run_logger(rl)
    log_event("node_start", {"node": "hotel_baseline_test", "n_sites": len(SITES)})

    print(f"\n{'='*70}")
    print(f"Hotel sites baseline test — crawl_list_tree on 5 OTAs")
    print(f"Params: {PARAMS}")
    print(f"Run log: {rl.path}")
    print(f"{'='*70}")

    overall_start = time.monotonic()
    results: list[dict] = []
    for label, url in SITES:
        r = await test_one(label, url)
        results.append(r)

    total_elapsed = time.monotonic() - overall_start

    out_path = HERE / "agent-workspace" / "diagnostics" / f"hotel-baseline-{int(time.time())}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "params": PARAMS,
        "total_elapsed_s": round(total_elapsed, 1),
        "results": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    log_event("node_complete", {
        "node": "hotel_baseline_test",
        "total_elapsed_s": round(total_elapsed, 1),
        "n_sites": len(SITES),
    })
    log_event("run_complete", {"completed": True})
    reset_run_logger(token)

    # Comparison table
    print(f"\n{'='*70}")
    print(f"SUMMARY (total {total_elapsed:.1f}s, results saved to {out_path.name})")
    print(f"{'='*70}")
    print(f"{'site':10} {'elapsed':>7} {'md_chars':>10} {'links':>10} {'children':>8} {'reasons'}")
    for r in results:
        rs = r.get("root_summary", {}) or {}
        elapsed = r.get("elapsed_s", "-")
        md = rs.get("markdown_chars", "-")
        lk = f"{rs.get('links_total', 0)}→{rs.get('links_kept', 0)}"
        ch = rs.get("children_count", "-")
        rsn = ",".join(rs.get("list_reasons", []) or [])
        if r.get("crashed") or r.get("tool_error"):
            print(f"{r['label']:10} {elapsed:>6}s  [ERROR] {r.get('error') or r.get('tool_error')}")
        else:
            print(f"{r['label']:10} {elapsed:>6}s  {md:>10,}  {lk:>10}  {ch:>8}  {rsn}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
