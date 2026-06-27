"""Summarize a run-log JSONL file: event counts, timings, tools, LLM calls.

Usage:
  python scripts/summarize_run_log.py agent-workspace/run-logs/<file>.log
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def main(path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"NOT FOUND: {p}")
        sys.exit(1)

    events = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                pass

    print("=" * 72)
    print(f"Log file: {p}")
    print(f"Size:     {p.stat().st_size / 1024:.1f} KB ({len(events)} events)")
    print("=" * 72)

    # 1. Event counts
    counts = Counter(e["event"] for e in events)
    print("\n── Event type counts ──")
    for ev, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {ev:25}  {n:4}")

    # 2. Node timings
    print("\n── Node timings ──")
    for e in events:
        if e["event"] == "node_complete":
            d = e["data"]
            extra = ""
            if "n_sources" in d:
                extra += f" sources={d['n_sources']}"
            if "n_portal_trees" in d:
                extra += f" trees={d['n_portal_trees']}"
            if "n_scored" in d:
                extra += f" scored={d['n_scored']}"
            if "report_n_sources" in d:
                extra += f" report_n={d['report_n_sources']}"
            print(f"  {d['node']:22}  {d['duration_ms']/1000:7.1f}s  cost=${d.get('cost_usd', 0):.4f}{extra}")

    # 3. Tool calls (count per tool)
    tool_calls = Counter()
    for e in events:
        if e["event"] == "tool_call":
            tool_calls[e["data"]["tool"]] += 1
    print("\n── Tool calls ──")
    for t, n in sorted(tool_calls.items(), key=lambda kv: -kv[1]):
        print(f"  {t:30}  {n:4}")

    # 4. LLM calls
    llm_calls = Counter()
    llm_costs: dict[str, float] = defaultdict(float)
    llm_total_tokens: dict[str, int] = defaultdict(int)
    llm_durations: dict[str, float] = defaultdict(float)
    for e in events:
        if e["event"] == "llm_call":
            llm_calls[e["data"]["purpose"]] += 1
        elif e["event"] == "llm_response":
            d = e["data"]
            p = d.get("purpose", "?")
            llm_costs[p] += float(d.get("cost_usd", 0) or 0)
            llm_total_tokens[p] += int(d.get("completion_tokens", 0) or 0) + int(d.get("prompt_tokens", 0) or 0)
            llm_durations[p] += float(d.get("latency_ms", 0) or 0) / 1000
    print("\n── LLM calls (purpose / count / total_tokens / wall_sec / cost) ──")
    for p, n in sorted(llm_calls.items(), key=lambda kv: -kv[1]):
        print(f"  {p:25}  {n:4}  tok={llm_total_tokens[p]:7}  {llm_durations[p]:6.1f}s  ${llm_costs[p]:.4f}")

    # 5. Fetch strategies
    fetch_outcomes: dict[tuple[str, str], int] = defaultdict(int)
    fetch_md_total = 0
    for e in events:
        if e["event"] == "fetch_strategy":
            d = e["data"]
            fetch_outcomes[(d["strategy"], d["outcome"])] += 1
            fetch_md_total += int(d.get("markdown_chars", 0) or 0)
    print("\n── Fetch strategies (strategy / outcome / count) ──")
    for (s, o), n in sorted(fetch_outcomes.items(), key=lambda kv: -kv[1]):
        print(f"  {s:12}  {o:20}  {n:4}")
    print(f"  Total markdown across all fetches: {fetch_md_total} chars ({fetch_md_total/1024:.1f} KB)")

    # 6. Reflect decisions
    print("\n── Reflect decisions ──")
    for e in events:
        if e["event"] == "reflect_decision":
            d = e["data"]
            print(f"  iter={d['iteration']}/{d['max_iterations']}  sufficient={d['is_sufficient']}  next={d['next_action']}  reason={d['reason']}")
            if d.get("gaps"):
                for g in d["gaps"][:5]:
                    print(f"    gap: {g}")

    # 7. Skill writes
    for e in events:
        if e["event"] == "tool_result" and e["data"]["tool"] == "propose_skill":
            s = e["data"]["summary"]
            print(f"\n── Skill proposed: {s['etld1']} / {s['pattern_id']} (conf={s['confidence']}, types={s['types']}) ──")
    for e in events:
        if e["event"] == "tool_result" and e["data"]["tool"] == "flush_skills":
            print(f"   → flush_skills: {e['data']['summary']['written']} written")

    # 8. Run total
    for e in events:
        if e["event"] == "run_complete":
            d = e["data"]
            print(f"\n── Run total ──")
            print(f"  Duration:   {d['total_duration_ms']/1000:.1f}s")
            print(f"  Cumulative cost: ${d.get('cumulative_cost_usd', 0):.4f}")
            print(f"  Stage timings:")
            for k, v in (d.get("stage_timings_seconds") or {}).items():
                print(f"    {k:22}  {v:6.1f}s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
