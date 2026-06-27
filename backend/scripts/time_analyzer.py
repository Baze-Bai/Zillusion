"""Detailed time-consumption analyzer for a discovery run log.

Decomposes each node's wall-clock time into:
  - LLM inference time (sum of llm_call → llm_response durations, by purpose)
  - Tool execution time (tool_call → tool_result, by tool)
  - Agent "thinking" gaps (tool_result → next tool_call) — only applies to
    agentic_discovery since other nodes have explicit llm_call/response.
  - Idle / overhead (the residual not accounted for by any of the above)

For the agentic_discovery node specifically, also bins:
  - Per-tool aggregate: count, total, avg, p50, p95
  - Per-fetch_strategy outcome (firecrawl/jina/httpx success/failure
    proportions + average fetch time)
  - crawl_list_tree internal time per page visited

Usage:
  python scripts/time_analyzer.py agent-workspace/run-logs/<file>.log
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _stat_block(durations: list[float]) -> str:
    if not durations:
        return "n=0"
    n = len(durations)
    total = sum(durations)
    avg = total / n
    p50 = statistics.median(durations)
    p95 = sorted(durations)[max(0, int(n * 0.95) - 1)] if n > 1 else durations[0]
    return f"n={n:3}  total={total:7.1f}s  avg={avg:5.2f}s  p50={p50:5.2f}s  p95={p95:5.2f}s"


def main(path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"NOT FOUND: {p}"); sys.exit(1)

    events = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not events:
        print("EMPTY LOG"); sys.exit(1)

    print("=" * 80)
    print(f"TIME ANALYSIS: {p.name}  ({p.stat().st_size/1024:.1f} KB, {len(events)} events)")
    print("=" * 80)

    # ── Per-node wall-clock time ──
    node_starts: dict[str, datetime] = {}
    node_durations: dict[str, float] = {}
    node_event_ranges: dict[str, tuple[int, int]] = {}  # node → (start_idx, end_idx)
    for i, e in enumerate(events):
        if e["event"] == "node_start":
            node_starts[e["data"]["node"]] = _parse_ts(e["ts"])
            node_event_ranges[e["data"]["node"]] = (i, len(events))  # default end
        elif e["event"] == "node_complete":
            node = e["data"]["node"]
            if node in node_starts:
                node_durations[node] = (_parse_ts(e["ts"]) - node_starts[node]).total_seconds()
                start_idx, _ = node_event_ranges[node]
                node_event_ranges[node] = (start_idx, i + 1)

    print("\n┌─ Per-node wall-clock ─────────────────────────────────────────────────┐")
    for node, dur in node_durations.items():
        print(f"│  {node:22}  {dur:7.1f}s")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── LLM time per purpose (across whole run) ──
    llm_durations: dict[str, list[float]] = defaultdict(list)
    pending_llm: dict[str, datetime] = {}  # purpose → ts
    for e in events:
        purpose = e.get("data", {}).get("purpose")
        if not purpose:
            continue
        if e["event"] == "llm_call":
            # Multiple parallel calls possible — use a list as stack
            pending_llm.setdefault(purpose, [])
            pending_llm[purpose].append(_parse_ts(e["ts"]))
        elif e["event"] == "llm_response":
            stack = pending_llm.get(purpose, [])
            if stack:
                start_ts = stack.pop(0)
                llm_durations[purpose].append((_parse_ts(e["ts"]) - start_ts).total_seconds())

    if llm_durations:
        print("\n┌─ LLM time by purpose ─────────────────────────────────────────────────┐")
        for purpose, durs in sorted(llm_durations.items(), key=lambda kv: -sum(kv[1])):
            print(f"│  {purpose:22}  {_stat_block(durs)}")
        total_llm = sum(d for ds in llm_durations.values() for d in ds)
        print(f"│  {'─' * 70}")
        print(f"│  TOTAL LLM (all purposes, parallel-collapsed):  {total_llm:7.1f}s wall-equiv")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Agentic-discovery internal decomposition ──
    agentic_range = node_event_ranges.get("agentic_discovery")
    if not agentic_range:
        print("\n(No agentic_discovery node found, skipping internal decomposition)")
        return

    start_idx, end_idx = agentic_range
    agentic_events = events[start_idx:end_idx]
    agentic_dur = node_durations.get("agentic_discovery", 0)

    # Tool execution time
    tool_durations: dict[str, list[float]] = defaultdict(list)
    pending_tools: dict[str, list[datetime]] = defaultdict(list)
    last_result_ts: datetime | None = None
    thinking_gaps: list[float] = []
    tool_call_ts_list: list[datetime] = []
    tool_result_ts_list: list[datetime] = []

    for e in agentic_events:
        if e["event"] == "tool_call":
            tool = e["data"]["tool"]
            pending_tools[tool].append(_parse_ts(e["ts"]))
            tool_call_ts_list.append(_parse_ts(e["ts"]))
            # Gap from last_result to now = thinking
            if last_result_ts is not None:
                gap = (_parse_ts(e["ts"]) - last_result_ts).total_seconds()
                if 0 < gap < 600:  # ignore impossible negative or absurd
                    thinking_gaps.append(gap)
        elif e["event"] == "tool_result":
            tool = e["data"]["tool"]
            if pending_tools[tool]:
                start_ts = pending_tools[tool].pop(0)
                dur = (_parse_ts(e["ts"]) - start_ts).total_seconds()
                tool_durations[tool].append(dur)
            last_result_ts = _parse_ts(e["ts"])
            tool_result_ts_list.append(last_result_ts)

    total_tool_time = sum(d for ds in tool_durations.values() for d in ds)
    total_thinking = sum(thinking_gaps)
    n_tool_calls = sum(len(ds) for ds in tool_durations.values())

    print(f"\n┌─ AGENTIC_DISCOVERY internal time decomposition ───────────────────────┐")
    print(f"│  Total wall-clock:        {agentic_dur:7.1f}s  (100%)")
    print(f"│  ├─ Tool execution:       {total_tool_time:7.1f}s  ({total_tool_time/agentic_dur*100:5.1f}%)")
    print(f"│  ├─ Agent thinking gaps:  {total_thinking:7.1f}s  ({total_thinking/agentic_dur*100:5.1f}%)")
    residual = agentic_dur - total_tool_time - total_thinking
    print(f"│  └─ Residual/overhead:    {residual:7.1f}s  ({residual/agentic_dur*100:5.1f}%)")
    print(f"│")
    print(f"│  Tool calls: {n_tool_calls} total, {len(thinking_gaps)} thinking gaps")
    print(f"│  Avg gap (LLM decision time): {total_thinking/max(len(thinking_gaps),1):.2f}s")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    print(f"\n┌─ Tool execution breakdown (within agentic) ───────────────────────────┐")
    for tool, durs in sorted(tool_durations.items(), key=lambda kv: -sum(kv[1])):
        share = sum(durs) / agentic_dur * 100
        print(f"│  {tool:25}  {_stat_block(durs)}  ({share:4.1f}% of agentic)")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Fetch strategy time ──
    fetch_strategy: dict[tuple[str, str], list[float]] = defaultdict(list)
    fetch_call_ts: dict[str, datetime] = {}  # url → when fetched
    for e in agentic_events:
        if e["event"] == "fetch_strategy":
            d = e["data"]
            key = (d.get("strategy", "?"), d.get("outcome", "?"))
            # We don't have explicit call/result timestamps for strategy,
            # but each fetch_strategy event marks the END of one attempt.
            # For total cost, sum tool_call→result for fetch_page.
            fetch_strategy[key].append(1.0)  # count placeholder

    if fetch_strategy:
        print(f"\n┌─ Fetch strategy outcomes (within agentic) ────────────────────────────┐")
        for (strat, outcome), durs in sorted(fetch_strategy.items(), key=lambda kv: -len(kv[1])):
            print(f"│  {strat:12}  {outcome:25}  n={len(durs):3}")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── crawl_list_tree internal ──
    crawl_nodes = [e for e in agentic_events if e["event"] == "crawl_node"]
    if crawl_nodes:
        print(f"\n┌─ crawl_list_tree internal stats ──────────────────────────────────────┐")
        # Count list vs leaf vs skipped per call
        per_kind: dict[str, int] = defaultdict(int)
        for cn in crawl_nodes:
            per_kind[cn["data"].get("page_kind", "?")] += 1
        print(f"│  Total node visits: {len(crawl_nodes)}")
        for k, v in per_kind.items():
            print(f"│    {k:10}  {v:3}")

        # crawl_list_tree call durations from tool_call/result already in tool_durations
        clt_durs = tool_durations.get("crawl_list_tree", [])
        if clt_durs:
            print(f"│  Per-call duration: {_stat_block(clt_durs)}")
            avg_per_visit = sum(clt_durs) / max(len(crawl_nodes), 1)
            print(f"│  Avg per node visit: {avg_per_visit:.2f}s")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Cumulative time breakdown (whole run) ──
    print(f"\n┌─ WHOLE-RUN TIME ATTRIBUTION ──────────────────────────────────────────┐")
    total_run = sum(node_durations.values())
    print(f"│  Total wall-clock (sum of nodes): {total_run:7.1f}s")
    print(f"│  ─────────────────────────────────────────")

    # Per-node attribution
    for node, dur in node_durations.items():
        if node == "agentic_discovery":
            # Decompose
            print(f"│  {node:22}  {dur:7.1f}s  ({dur/total_run*100:4.1f}%)")
            print(f"│    ├─ tool exec:        {total_tool_time:7.1f}s  ({total_tool_time/dur*100:4.1f}% of node)")
            print(f"│    ├─ agent thinking:   {total_thinking:7.1f}s  ({total_thinking/dur*100:4.1f}% of node)")
            print(f"│    └─ overhead:         {residual:7.1f}s  ({residual/dur*100:4.1f}% of node)")
        else:
            # LLM time within this node
            node_llm_time = 0.0
            for purpose, durs in llm_durations.items():
                # Map purpose to node (heuristic)
                if (
                    (node == "parse_intent" and purpose == "intent_parser") or
                    (node == "judge" and purpose.startswith("judge_")) or
                    (node == "reflect" and purpose == "reflect") or
                    (node == "finalize" and purpose == "finalize")
                ):
                    node_llm_time += sum(durs)
            print(f"│  {node:22}  {dur:7.1f}s  ({dur/total_run*100:4.1f}%)")
            if node_llm_time > 0:
                share = node_llm_time / dur * 100
                print(f"│    └─ LLM time:         {node_llm_time:7.1f}s  ({share:4.1f}% of node)")
    print(f"└──────────────────────────────────────────────────────────────────────┘")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
