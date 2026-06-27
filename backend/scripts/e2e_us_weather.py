"""E2E run for query '搜集美国天气数据' with per-stage + per-tool timing breakdown.

Drives the same LangGraph as /api/v1/discover. After the run completes,
parses the run log JSONL to produce:

  - Per-stage wall time (parse_intent / agentic_discovery / judge / ...)
  - Sub-stage timings inside judge (stage_a.* / stage_b.*)
  - Per-tool latency: total time, count, mean, p50, max
  - Per-LLM-purpose latency: total time, count, mean, p50, max
  - Top-N slowest individual calls

Output: prints a structured breakdown to stdout + writes the same breakdown
to ``agent-workspace/timing-reports/<query_id>.md`` for later review.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE / ".env")

# Host-side override for firecrawl when running from the Windows host.
os.environ["SEARCH_FIRECRAWL_SELF_HOSTED_URL"] = "http://localhost:3002"

from src.agents.graph import get_graph  # noqa: E402
from src.services.run_logger import (  # noqa: E402
    RunLogger, log_event, reset_run_logger, set_run_logger,
)


QUERY_TEXT = "搜集美国天气数据"


async def run_pipeline() -> tuple[str, Path, float, dict[str, float], list]:
    """Run the full graph; return (query_id, run_log_path, total_s, node_durations, scored_sources)."""
    query_id = "uswx-" + uuid.uuid4().hex[:6]

    rl = RunLogger(query_id=query_id, query_text=QUERY_TEXT)
    token = set_run_logger(rl)

    print("=" * 72)
    print(f"Query:        {QUERY_TEXT}")
    print(f"Query id:     {query_id}")
    print(f"Run log:      {rl.path}")
    print("=" * 72)
    print()

    log_event("run_config", {
        "request_id": f"e2e-{query_id}",
        "query": QUERY_TEXT,
        "max_iterations": 1,
        "license_constraint": "any",
        "budget_constraint": "any",
    })

    graph = get_graph()
    initial_state = {
        "query": QUERY_TEXT,
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
             "skill_writeback", "finalize"}

    start = time.monotonic()
    node_durations: dict[str, float] = {}
    final_sources: list = []

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
                    print(f"           sources={len(srcs)}, trees={len(trees)}")
                elif en == "judge":
                    scored = output.get("scored_sources") or []
                    nc_summary["n_scored"] = len(scored)
                    print(f"           scored={len(scored)}")
                    final_sources = scored
                log_event("node_complete", {"node": en, **nc_summary})

            elif ek == "on_custom_event" and en == "task_description_committed":
                payload = event.get("data") or {}
                t = time.monotonic() - start
                print(f"[{t:6.1f}s] task_description.md committed "
                      f"({payload.get('chars', 0)} chars)")

    except Exception as e:
        print(f"\nGRAPH FAILED: {type(e).__name__}: {e}")
        log_event("error", {"scope": "graph_run", "error_type": type(e).__name__,
                            "message": str(e)})

    total_s = time.monotonic() - start
    log_event("run_complete", {
        "completed": True,
        "nodes_executed": list(node_durations.keys()),
        "total_duration_ms": round(total_s * 1000, 2),
        "stage_timings_seconds": {k: round(v, 3) for k, v in node_durations.items()},
    })
    reset_run_logger(token)

    return query_id, rl.path, total_s, node_durations, final_sources


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile, returns 0 for empty list."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _cluster_wall_sum(items: list[tuple[float, float]], gap_s: float = 1.0) -> float:
    """Estimate wall time when N calls run in parallel by clustering them.

    items: list of (ts_epoch, latency_s) per call.
    Cluster = consecutive (by start ts) calls within ``gap_s`` of each
    other; cluster wall = max latency within the cluster; total wall =
    sum across clusters. Crude but works well in practice — when 10
    calls fire near-simultaneously and finish within the slowest one,
    they all land in one cluster with wall = max(latencies).
    """
    if not items:
        return 0.0
    sorted_items = sorted(items, key=lambda x: x[0])
    clusters: list[list[float]] = [[sorted_items[0][1]]]
    last_ts = sorted_items[0][0]
    for ts, lat in sorted_items[1:]:
        if ts - last_ts <= gap_s:
            clusters[-1].append(lat)
        else:
            clusters.append([lat])
        last_ts = ts
    return sum(max(c) for c in clusters)


def analyze_run_log(path: Path, run_total_wall_s: float | None = None) -> str:
    """Walk the JSONL run log and produce a timing breakdown markdown.

    Emits two views per row:
      - **Wall (s)** — the time actually consumed in this run. For tools,
        we use ``summary.elapsed_ms`` when present (tool reports its own
        wall via time.monotonic delta). For LLM purposes we estimate via
        cluster-based wall: calls within 1s of each other on the timeline
        are treated as one parallel cluster whose wall = max latency.
      - **Sum (s)** — naive sum of per-call latencies. For sequential tools
        this equals Wall; for tools/LLMs that fire many parallel sub-calls
        the Sum overstates wall significantly.

    Tools that fan-out internally (search_web with queries=[...], fetch_page
    with urls=[...]) also write ``elapsed_ms_sum`` into summary; we surface
    that as a third column ``Sum-internal (s)`` for those rows so you can
    see the parallelism factor (Sum-internal / Wall).
    """
    if not path.exists():
        return f"(run log {path} not found)"

    from datetime import datetime as _dt

    # Per-tool stats:
    #   walls[tool]  → list of per-call wall times (real elapsed)
    #   sums[tool]   → list of per-call internal-parallel sums (when present)
    tool_walls: dict[str, list[float]] = defaultdict(list)
    tool_sums: dict[str, list[float]] = defaultdict(list)
    # Per-LLM-purpose: ts_epoch + latency for cluster-based wall estimation
    llm_events: dict[str, list[tuple[float, float]]] = defaultdict(list)
    llm_latencies: dict[str, list[float]] = defaultdict(list)
    sub_stage_timings: dict[str, float] = {}
    total_events = 0
    node_completes: list[dict] = []
    run_config: dict = {}
    run_complete: dict = {}
    open_calls_per_tool: dict[str, list[float]] = defaultdict(list)

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_events += 1
            ev = rec.get("event", "")
            data = rec.get("data", {}) or {}
            ts = rec.get("ts", "")
            try:
                ts_epoch = _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts_epoch = 0.0

            if ev == "tool_call":
                tool = data.get("tool", "unknown")
                open_calls_per_tool[tool].append(ts_epoch)
            elif ev == "tool_result":
                tool = data.get("tool", "unknown")
                summary = data.get("summary", {}) or {}
                # Per-call wall. Prefer tool-reported elapsed_ms.
                if isinstance(summary, dict) and "elapsed_ms" in summary:
                    wall = float(summary["elapsed_ms"]) / 1000.0
                else:
                    starts = open_calls_per_tool.get(tool, [])
                    if starts and ts_epoch:
                        wall = ts_epoch - starts.pop(0)
                    else:
                        wall = 0.0
                tool_walls[tool].append(wall)
                # Per-call internal-parallel sum (only present for tools
                # that fan-out internally — search_web, fetch_page, etc.)
                if isinstance(summary, dict) and "elapsed_ms_sum" in summary:
                    tool_sums[tool].append(float(summary["elapsed_ms_sum"]) / 1000.0)
            elif ev == "tool_error":
                tool = data.get("tool", "unknown")
                tool_walls[tool + " [error]"].append(0.0)
            elif ev == "llm_response":
                purpose = data.get("purpose", "unknown")
                latency_s = float(data.get("latency_ms", 0) or 0) / 1000.0
                llm_events[purpose].append((ts_epoch, latency_s))
                llm_latencies[purpose].append(latency_s)
            elif ev == "node_complete":
                node_completes.append(data)
                ts_dict = data.get("stage_timings", {}) or {}
                if isinstance(ts_dict, dict):
                    for k, v in ts_dict.items():
                        if "." in k:
                            sub_stage_timings[k] = float(v or 0)
            elif ev == "run_config":
                run_config = data
            elif ev == "run_complete":
                run_complete = data

    lines: list[str] = []
    lines.append("# Per-stage + per-tool timing breakdown\n\n")
    lines.append(f"Query: `{run_config.get('query', '?')}`\n\n")
    lines.append(f"Run log: `{path}`\n\n")
    lines.append(f"Total log events: {total_events}\n\n")

    if run_total_wall_s is not None:
        lines.append(f"**Run-level wall time: {run_total_wall_s:.1f}s** "
                     "(real elapsed from start to last node_complete)\n\n")

    # ── Per-stage ────────────────────────────────────────────────────
    lines.append("## Stage-level wall timings\n\n")
    lines.append("Stage wall = wall time of that LangGraph node (`time.monotonic` "
                 "start to end). Cost is cumulative-at-end-of-stage.\n\n")
    lines.append("| Stage | Wall (s) | Cost ($) | Output summary |\n")
    lines.append("|---|---:|---:|---|\n")
    for nc in node_completes:
        node = nc.get("node", "?")
        dur_ms = float(nc.get("duration_ms", 0) or 0)
        cost = float(nc.get("cost_usd", 0) or 0)
        extras = []
        for k in ("n_sources", "n_portal_trees", "n_scored",
                  "report_n_sources_total", "report_n_api",
                  "report_n_file", "report_n_embedded"):
            if k in nc:
                extras.append(f"{k}={nc[k]}")
        lines.append(f"| {node} | {dur_ms/1000:.2f} | {cost:.4f} | {', '.join(extras)} |\n")

    if sub_stage_timings:
        lines.append("\n## Judge sub-stages (wall)\n\n")
        lines.append("| Sub-stage | Wall (s) |\n|---|---:|\n")
        for k in sorted(sub_stage_timings.keys()):
            lines.append(f"| {k} | {sub_stage_timings[k]:.2f} |\n")

    # ── Per-tool: WALL vs SUM vs Sum-internal ────────────────────────
    lines.append("\n## Per-tool — Wall vs Sum\n\n")
    lines.append("- **Wall**: real time the tool consumed in this run "
                 "(sum of per-call elapsed_ms). Single-source-of-truth column.\n")
    lines.append("- **Sum-internal**: when the tool fans out N parallel sub-calls "
                 "internally (e.g. search_web with queries=[...]), this is the "
                 "sum of those sub-call latencies. Wall ≈ Sum-internal / N when "
                 "fan-out is well-balanced. Blank when the tool doesn't fan out.\n\n")
    lines.append("| Tool | Calls | **Wall (s)** | Sum-internal (s) | Mean wall (s) | Max wall (s) |\n")
    lines.append("|---|---:|---:|---:|---:|---:|\n")
    tool_rows = []
    for t, walls in tool_walls.items():
        wall_sum = sum(walls)
        sum_int = sum(tool_sums.get(t) or [])
        mean_wall = statistics.mean(walls) if walls else 0
        max_wall = max(walls) if walls else 0
        tool_rows.append((t, len(walls), wall_sum, sum_int, mean_wall, max_wall))
    tool_rows.sort(key=lambda r: -r[2])
    for t, n, wall, sum_int, mean_w, max_w in tool_rows:
        sum_int_cell = f"{sum_int:.1f}" if sum_int > 0 else "-"
        lines.append(f"| {t} | {n} | **{wall:.1f}** | {sum_int_cell} | {mean_w:.2f} | {max_w:.2f} |\n")
    grand_tool_wall = sum(sum(v) for v in tool_walls.values())
    lines.append(f"\n**Grand total per-tool wall: {grand_tool_wall:.1f}s**\n")

    # ── Per-LLM-purpose: WALL (cluster-based estimate) vs SUM ────────
    lines.append("\n## Per-LLM-purpose — Wall (cluster est) vs Sum\n\n")
    lines.append("- **Wall (est)**: calls within 1s of each other on the timeline "
                 "are treated as one parallel cluster; cluster wall = max latency; "
                 "total wall = sum across clusters. Approximates actual time "
                 "consumed in the run.\n")
    lines.append("- **Sum**: naive sum of per-call latencies. Equal to Wall for "
                 "purely sequential purposes; can be 5-10x Wall when calls fan out "
                 "in parallel (e.g. classify_urls helper fires 10+ LLMs at once).\n\n")
    lines.append("| Purpose | Calls | **Wall (s)** | Sum (s) | Parallelism | Mean (s) | Max (s) |\n")
    lines.append("|---|---:|---:|---:|---:|---:|---:|\n")
    llm_rows = []
    for p, events in llm_events.items():
        lats = llm_latencies.get(p, [])
        sum_lat = sum(lats)
        wall_est = _cluster_wall_sum(events, gap_s=1.0)
        parallelism = sum_lat / wall_est if wall_est > 0 else 1.0
        mean = statistics.mean(lats) if lats else 0
        mx = max(lats) if lats else 0
        llm_rows.append((p, len(lats), wall_est, sum_lat, parallelism, mean, mx))
    llm_rows.sort(key=lambda r: -r[2])  # sort by wall
    for p, n, wall, summ, paral, mean, mx in llm_rows:
        lines.append(f"| {p} | {n} | **{wall:.1f}** | {summ:.1f} | {paral:.1f}x | {mean:.2f} | {mx:.2f} |\n")
    grand_llm_wall = sum(r[2] for r in llm_rows)
    grand_llm_sum = sum(r[3] for r in llm_rows)
    lines.append(f"\n**Grand total LLM wall (est): {grand_llm_wall:.1f}s** "
                 f"(sum-equivalent: {grand_llm_sum:.1f}s)\n")

    # ── Slowest individual calls (raw latency, for hotspot picking) ──
    lines.append("\n## 10 slowest individual tool calls\n\n")
    individual = []
    for t, walls in tool_walls.items():
        for w in walls:
            individual.append((t, w))
    individual.sort(key=lambda x: -x[1])
    lines.append("| Rank | Tool | Wall (s) |\n|---:|---|---:|\n")
    for i, (t, w) in enumerate(individual[:10], 1):
        lines.append(f"| {i} | {t} | {w:.2f} |\n")

    lines.append("\n## 10 slowest individual LLM calls\n\n")
    individual_llm = []
    for p, lats in llm_latencies.items():
        for lat in lats:
            individual_llm.append((p, lat))
    individual_llm.sort(key=lambda x: -x[1])
    lines.append("| Rank | Purpose | Latency (s) |\n|---:|---|---:|\n")
    for i, (p, lat) in enumerate(individual_llm[:10], 1):
        lines.append(f"| {i} | {p} | {lat:.2f} |\n")

    return "".join(lines)


async def main() -> None:
    query_id, log_path, total_s, node_durations, scored = await run_pipeline()

    print()
    print("=" * 72)
    print(f"DONE in {total_s:.1f}s — analyzing run log...")
    print("=" * 72)

    report_md = analyze_run_log(log_path, run_total_wall_s=total_s)
    out_dir = HERE / "agent-workspace" / "timing-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{query_id}.md"
    out_path.write_text(report_md, encoding="utf-8")

    print(f"\nReport written to: {out_path}")
    print()
    print("=" * 72)
    print(report_md)


if __name__ == "__main__":
    asyncio.run(main())
