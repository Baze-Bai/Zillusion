"""E2E graph run with an EMBEDDED hotel-listings query (skill-hit demo).

Same harness as ``e2e_full_graph.py`` but the query is steered toward
*embedded* hotel-listing pages (agoda / booking / ctrip / trip city
listing pages) instead of JSON APIs. Those URL shapes are exactly the
ones the domain-skill library already covers, so ``classify_urls``
should produce live ``from_skill=True`` hits — demonstrating the Phase 5
deterministic skill lookup firing in a real run.
"""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE / ".env")

import os  # noqa: E402
os.environ["SEARCH_FIRECRAWL_SELF_HOSTED_URL"] = "http://localhost:3002"

from src.agents.graph import get_graph  # noqa: E402
from src.services.run_logger import (  # noqa: E402
    RunLogger, log_event, reset_run_logger, set_run_logger,
)


async def main() -> None:
    query_id = "v3list-" + uuid.uuid4().hex[:6]
    # EMBEDDED steer: ask for hotel LISTING PAGES (web-embedded data), not
    # APIs and not file downloads. This pushes discovery toward the OTA
    # city-listing pages (agoda/booking/ctrip/trip) whose URL shapes the
    # skill library already knows — so classify_urls should hit the library.
    query_text = (
        "我需要北京酒店的列表数据,字段含酒店名、地址、价格、评分。"
        "数据就在网页上(嵌入式数据/列表页即可),"
        "不要 JSON API,也不要文件下载——直接是携程/booking/agoda/trip 这类"
        "OTA 的北京酒店列表页面。"
    )

    rl = RunLogger(query_id=query_id, query_text=query_text)
    token = set_run_logger(rl)

    print("=" * 72)
    print(f"Query:        {query_text}")
    print(f"Query id:     {query_id}")
    print(f"Run log:      {rl.path}")
    print("=" * 72)
    print()

    log_event("run_config", {
        "request_id": f"e2e-{query_id}",
        "query": query_text,
        "max_iterations": 1,
        "license_constraint": "any",
        "budget_constraint": "any",
    })

    graph = get_graph()
    initial_state = {
        "query": query_text,
        "user_id": "anonymous",
        "request_id": f"e2e-{query_id}",
        "query_id": query_id,
        "iteration": 0,
        "max_iterations": 1,
        "cost_accumulated": 0.0,
        "stage_timings": {},
        "errors": [],
        "portal_budget": {
            "max_portals_to_map": 6,
            "max_scrape_per_portal": 5,
            "max_total_scrape": 20,
            "portals_mapped": 0,
            "total_scraped": 0,
        },
    }
    config = {"configurable": {"thread_id": query_id}}

    nodes = {"parse_intent", "agentic_discovery", "judge", "reflect",
             "diagnostics_writeback", "finalize"}

    start = time.monotonic()
    cumulative_cost = 0.0
    node_durations: dict[str, float] = {}
    final_sources: list = []
    final_report = None

    try:
        async for event in graph.astream_events(initial_state, config=config, version="v2"):
            ek = event.get("event", "")
            en = event.get("name", "")

            if ek == "on_chain_start" and en in nodes:
                t = time.monotonic() - start
                print(f"[{t:6.1f}s] START {en}")
                log_event("node_start", {"node": en})

            elif ek == "on_chain_end" and en in nodes:
                output = event.get("data", {}).get("output", {}) or {}
                timings = output.get("stage_timings", {}) or {}
                duration = float(timings.get(en, 0) or 0)
                cost = float(output.get("cost_accumulated", 0) or 0)
                t = time.monotonic() - start
                print(f"[{t:6.1f}s] END   {en}: {duration:.1f}s cum_cost=${cost:.4f}")

                cumulative_cost = max(cumulative_cost, cost)
                node_durations[en] = duration

                nc_summary: dict = {
                    "duration_ms": round(duration * 1000, 2),
                    "cost_usd": cost,
                    "output_keys": list(output.keys()),
                }
                if en == "agentic_discovery":
                    srcs = output.get("deduplicated_sources") or []
                    trees = output.get("portal_trees") or []
                    nc_summary["n_sources"] = len(srcs)
                    nc_summary["n_portal_trees"] = len(trees)
                    print(f"           → {len(srcs)} sources, {len(trees)} portal trees")
                elif en == "judge":
                    scored = output.get("scored_sources") or []
                    nc_summary["n_scored"] = len(scored)
                    print(f"           → {len(scored)} scored sources")
                    final_sources = scored
                elif en == "finalize":
                    rep = output.get("final_report")
                    if rep is not None:
                        final_report = rep
                        try:
                            nc_summary["report_n_sources_total"] = int(
                                getattr(rep, "total_found", 0) or 0
                            )
                            nc_summary["report_n_ranked"] = len(
                                getattr(rep, "all_sources_ranked", []) or []
                            )
                            nc_summary["report_n_api"] = len(
                                getattr(rep, "api_sources", []) or []
                            )
                            nc_summary["report_n_file"] = len(
                                getattr(rep, "file_sources", []) or []
                            )
                            nc_summary["report_n_embedded"] = len(
                                getattr(rep, "embedded_sources", []) or []
                            )
                        except Exception:
                            pass
                log_event("node_complete", {"node": en, **nc_summary})
    except Exception as e:
        print(f"\nGRAPH FAILED: {type(e).__name__}: {e}")
        log_event("error", {"scope": "graph_run", "error_type": type(e).__name__,
                            "message": str(e), "traceback": traceback.format_exc()})

    total_s = time.monotonic() - start
    log_event("run_complete", {
        "completed": True,
        "nodes_executed": list(node_durations.keys()),
        "total_duration_ms": round(total_s * 1000, 2),
        "cumulative_cost_usd": round(cumulative_cost, 6),
        "stage_timings_seconds": {k: round(v, 3) for k, v in node_durations.items()},
    })
    reset_run_logger(token)

    try:
        from src.services.mcp_client import mcp_client
        await mcp_client.disconnect_all()
    except Exception:
        pass

    print()
    print("=" * 72)
    print(f"DONE in {total_s:.1f}s")
    print(f"Cumulative cost:   ${cumulative_cost:.4f}")
    print(f"Final sources:     {len(final_sources)}")
    print("Per-node timings:")
    for n, d in node_durations.items():
        print(f"  {n:22}  {d:6.1f}s")
    print(f"Run log:           {rl.path}")
    print("=" * 72)

    if final_report is not None:
        try:
            ranked = getattr(final_report, "all_sources_ranked", []) or []
            api_s = getattr(final_report, "api_sources", []) or []
            file_s = getattr(final_report, "file_sources", []) or []
            emb_s = getattr(final_report, "embedded_sources", []) or []
            print(f"\nFinal report: total_found={getattr(final_report, 'total_found', '?')}")
            print(f"  api_sources:      {len(api_s)}")
            print(f"  file_sources:     {len(file_s)}")
            print(f"  embedded_sources: {len(emb_s)}")
            print(f"  all_sources_ranked: {len(ranked)}")
            print("\nTop 10 ranked sources:")
            for i, s in enumerate(ranked[:10], 1):
                name = getattr(s, "name", "")
                stype = getattr(s, "source_type", None)
                stype_v = stype.value if hasattr(stype, "value") else str(stype)
                url = getattr(s, "url", "")
                scores = getattr(s, "scores", None)
                overall = getattr(scores, "overall_score", "?") if scores else "?"
                print(f"  [{i}] [{stype_v:8}] score={overall}  {name}")
                print(f"      {url}")
        except Exception as e:
            print(f"  (could not enumerate sources: {e})")


if __name__ == "__main__":
    asyncio.run(main())
