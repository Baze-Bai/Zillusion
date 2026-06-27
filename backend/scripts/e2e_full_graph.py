"""Full 6-node graph E2E test bypassing HTTP/uvicorn.

Drives the same LangGraph that ``/api/v1/discover`` invokes, but directly
from Python so we can run it with a single command and capture the
complete run log + final report.

Replicates the logging hooks from ``api/routes/discover.py`` (run_config,
node_start, node_complete with summary, run_complete with cumulative
totals) so the resulting run-log file has the same shape as a real
HTTP-driven run.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Load .env into os.environ — pydantic-settings reads .env into the
# Settings instance, but DEEPSEEK_API_KEY (used by the Agent SDK
# subprocess via os.environ.get) needs the var actually exported. Same
# for SEARCH_FIRECRAWL_SELF_HOSTED_URL (we override below for host run).
from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE / ".env")

# Host-side override: .env points to docker-internal "firecrawl" hostname;
# when running this script from the Windows host we need localhost.
import os  # noqa: E402
os.environ["SEARCH_FIRECRAWL_SELF_HOSTED_URL"] = "http://localhost:3002"

from src.agents.graph import get_graph  # noqa: E402
from src.services.run_logger import (  # noqa: E402
    RunLogger, log_event, reset_run_logger, set_run_logger,
)


async def main() -> None:
    query_id = "v3test-" + uuid.uuid4().hex[:6]
    # Query designed to exercise all v3 changes:
    #   - license_constraint=commercial (license veto path)
    #   - desired_formats=JSON+API (format_fit dimension)
    #   - target_schema={hotel_name, address, price} (schema_coverage dim)
    #   - geographic_scope=Beijing (geographic_fit dimension)
    #   - topic=hotels (Stage-A relevance + binary filter)
    query_text = "我需要可商用的北京酒店数据,要 JSON API,字段含酒店名、地址、价格"

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
        "max_iterations": 1,   # no reflect-loop so the test stays bounded
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
             "skill_writeback", "diagnostics_writeback", "finalize"}

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
                        # FinalReport has no `sources` attribute — it
                        # exposes `total_found` + `all_sources_ranked`
                        # + per-type {api,file,embedded}_sources lists.
                        # Mirror the same fields discover.py logs so the
                        # run log accurately reflects the final shape.
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

    # Best-effort MCP teardown — close stdio sessions inside this loop so the
    # one-shot script doesn't emit an anyio "cancel scope in a different task"
    # race on interpreter exit (cosmetic, but it would otherwise surface as a
    # post-run traceback in any harness that greps stderr).
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
    print(f"Per-node timings:")
    for n, d in node_durations.items():
        print(f"  {n:22}  {d:6.1f}s")
    print(f"Run log:           {rl.path}")
    print(f"Run log size:      {rl.path.stat().st_size / 1024:.1f} KB")
    print("=" * 72)

    if final_report is not None:
        # FinalReport groups by type (api/file/embedded) plus a unified
        # ``all_sources_ranked``. Previous script looked at ``sources``
        # which doesn't exist; print all four buckets explicitly.
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
