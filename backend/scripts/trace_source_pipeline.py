"""Extract the data-source pipeline from a run log:

Stage 1: search_web — which queries fired (URLs not in log, only counts)
Stage 2: probe_url — URLs the agent decided were worth a HEAD check
Stage 3: fetch_page — URLs the agent decided to deep-read
Stage 4: agent_done — N sources emitted by the agentic node
Stage 5: judge_stage_a — batch filter URLs
Stage 6: judge_stage_b — per-source 5-dim scoring URLs (the survivors)
Stage 7: final_report — what made it to the user
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from collections import OrderedDict


def main(path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"NOT FOUND: {p}"); sys.exit(1)

    events = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    print("=" * 78)
    print(f"PIPELINE: {p.name}")
    print("=" * 78)

    # Stage 1: search_web queries (URLs not retained, just queries)
    sw_queries = []
    sw_total_results = 0
    for e in events:
        if e["event"] == "tool_call" and e["data"]["tool"] == "search_web":
            sw_queries.append(e["data"]["input"].get("query", ""))
        if e["event"] == "tool_result" and e["data"]["tool"] == "search_web":
            sw_total_results += int(e["data"]["summary"].get("n_results", 0))
    print(f"\n── STAGE 1: search_web (web discovery) ──")
    print(f"  {len(sw_queries)} queries → {sw_total_results} total candidate URLs (not retained in log)")
    print(f"  ⚠️  Gap: search_web tool_result only logs n_results, not the actual URLs.")
    print(f"      Add `urls_sample` to its summary if you want them traceable.")

    # Stage 2: probe_url URLs
    probed_urls = OrderedDict()
    for e in events:
        if e["event"] == "tool_call" and e["data"]["tool"] == "probe_url":
            u = e["data"]["input"].get("url", "")
            if u: probed_urls[u] = None
        if e["event"] == "tool_result" and e["data"]["tool"] == "probe_url":
            s = e["data"]["summary"]
            u = s.get("url", "")
            probed_urls[u] = (s.get("is_alive"), s.get("status_code"), s.get("content_type"))
    print(f"\n── STAGE 2: probe_url (cheap liveness check) ──")
    print(f"  {len(probed_urls)} URLs probed (HEAD with GET fallback):")
    alive_ct, dead_ct = 0, 0
    for u, r in probed_urls.items():
        if r is None: continue
        alive, status, ct = r
        marker = "✓" if alive else "✗"
        if alive: alive_ct += 1
        else: dead_ct += 1
        print(f"    {marker} [{status}] {u[:80]}")
    print(f"  → {alive_ct} alive / {dead_ct} dead")

    # Stage 3: fetch_page URLs (deeper inspection)
    fetched_urls = OrderedDict()
    for e in events:
        if e["event"] == "tool_call" and e["data"]["tool"] == "fetch_page":
            u = e["data"]["input"].get("url", "")
            if u: fetched_urls[u] = None
        if e["event"] == "tool_result" and e["data"]["tool"] == "fetch_page":
            s = e["data"]["summary"]
            u = s.get("url", "")
            fetched_urls[u] = s.get("markdown_chars", 0)
    print(f"\n── STAGE 3: fetch_page (deep markdown read) ──")
    print(f"  {len(fetched_urls)} URLs fetched:")
    for u, md_chars in fetched_urls.items():
        size_marker = ("EMPTY" if not md_chars else f"{md_chars/1024:.1f}KB")
        print(f"    [{size_marker:>7}] {u[:80]}")

    # Stage 4: agentic_discovery output (count only — final_text not in this log)
    agentic_out = None
    for e in events:
        if e["event"] == "node_complete" and e["data"].get("node") == "agentic_discovery":
            agentic_out = e["data"]
    if agentic_out:
        print(f"\n── STAGE 4: agentic_discovery emits ──")
        print(f"  n_sources       = {agentic_out.get('n_sources')}")
        print(f"  n_portal_trees  = {agentic_out.get('n_portal_trees')}")
        print(f"  cost_usd        = ${agentic_out.get('cost_usd', 0):.4f}")
        print(f"  duration        = {agentic_out.get('duration_ms', 0)/1000:.1f}s")
        print(f"  ⚠️  Gap: full source list not in log (predates final_text fix).")
        print(f"      With current logger, future runs would have it via agent_done.final_text.")

    # Stage 5: judge_stage_a URLs (batch Haiku filter)
    # The prompt contains the URLs of all candidates in this batch
    sa_urls = OrderedDict()
    url_re = re.compile(r"https?://[^\s\"<>]+")
    for e in events:
        if e["event"] == "llm_call" and e["data"].get("purpose") == "judge_stage_a":
            prompt = e["data"].get("prompt_excerpt", "")
            for u in url_re.findall(prompt):
                # Stop at trailing punctuation
                u = u.rstrip(".,;)\\\"")
                if u not in sa_urls: sa_urls[u] = None
    print(f"\n── STAGE 5: judge_stage_a (Haiku batch filter, relevance only) ──")
    print(f"  {len(sa_urls)} URLs entered batch evaluation (from {sum(1 for e in events if e['event']=='llm_call' and e['data'].get('purpose')=='judge_stage_a')} LLM calls)")
    for u in list(sa_urls.keys())[:25]:
        print(f"    {u[:80]}")
    if len(sa_urls) > 25:
        print(f"    ... and {len(sa_urls)-25} more")

    # Stage 6: judge_stage_b URLs (Sonnet 5-dim scoring of survivors)
    sb_urls = OrderedDict()
    for e in events:
        if e["event"] == "llm_call" and e["data"].get("purpose") == "judge_stage_b":
            prompt = e["data"].get("prompt_excerpt", "")
            # The stage B prompt is one source per call — look for "URL: ..."
            m = re.search(r"-\s*URL:\s*(\S+)", prompt)
            if m:
                u = m.group(1).rstrip(".,;)\\\"")
                if u not in sb_urls: sb_urls[u] = None
    print(f"\n── STAGE 6: judge_stage_b (Sonnet 5-dim per-source scoring) ──")
    print(f"  {len(sb_urls)} URLs survived to stage B (one LLM call each):")
    for u in sb_urls:
        print(f"    {u[:80]}")

    # Stage 7: final report (from node_complete.finalize)
    finalize_out = None
    for e in events:
        if e["event"] == "node_complete" and e["data"].get("node") == "finalize":
            finalize_out = e["data"]
    print(f"\n── STAGE 7: final_report (passes through finalize) ──")
    if finalize_out:
        print(f"  report_n_sources   = {finalize_out.get('report_n_sources', '?')}")
        print(f"  finalize duration  = {finalize_out.get('duration_ms', 0)/1000:.1f}s")

    # Summary funnel
    print(f"\n── FUNNEL SUMMARY ──")
    print(f"  Web candidates:        ~{sw_total_results} URLs (search_web)")
    print(f"  Worth probing:         {len(probed_urls)} URLs (cheap HEAD)")
    print(f"  Worth fetching:        {len(fetched_urls)} URLs (Firecrawl)")
    print(f"  Agent emits:           {agentic_out.get('n_sources') if agentic_out else '?'} sources")
    print(f"  Stage-A judge filter:  {len(sa_urls)} URLs entered (Haiku relevance)")
    print(f"  Stage-B scored:        {len(sb_urls)} URLs (Sonnet 5-dim)")
    print(f"  Final report:          {finalize_out.get('report_n_sources', '?') if finalize_out else '?'} sources")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
