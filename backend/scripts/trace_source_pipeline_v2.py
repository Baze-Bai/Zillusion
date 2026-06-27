"""Trace data source evolution + per-stage timing from a run log.

Uses the FULL (untruncated) log fields added recently:
  - llm_call.messages    (full system+user+history)
  - llm_response.response (full output)
  - agent_done.final_text (full final JSON)
  - search_web.tool_result.summary.results (full url+title+snippet)
  - crawl_node.links_raw / links_kept_list / prefilter_stats

Usage:
  python scripts/trace_source_pipeline_v2.py agent-workspace/run-logs/<file>.log
"""
from __future__ import annotations

import json
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _humanize_dt(start: datetime, end: datetime) -> str:
    d = (end - start).total_seconds()
    if d < 1:
        return f"{d*1000:.0f}ms"
    if d < 60:
        return f"{d:.1f}s"
    return f"{d//60:.0f}m{d%60:04.1f}s"


def main(path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"NOT FOUND: {p}")
        sys.exit(1)

    events = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not events:
        print("EMPTY LOG"); sys.exit(1)

    run_start_ts = _parse_ts(events[0]["ts"])

    print("=" * 80)
    print(f"PIPELINE TRACE: {p.name}  ({p.stat().st_size/1024:.1f} KB, {len(events)} events)")
    print("=" * 80)

    # ── Per-stage timing breakdown ──
    print(f"\n┌─ TIMING ─────────────────────────────────────────────────────────────┐")
    node_starts = {}
    for e in events:
        if e["event"] == "node_start":
            node_starts[e["data"]["node"]] = _parse_ts(e["ts"])
        elif e["event"] == "node_complete":
            node = e["data"]["node"]
            d_ms = e["data"].get("duration_ms", 0)
            extra = []
            if "n_sources" in e["data"]:
                extra.append(f"sources={e['data']['n_sources']}")
            if "n_portal_trees" in e["data"]:
                extra.append(f"trees={e['data']['n_portal_trees']}")
            if "n_scored" in e["data"]:
                extra.append(f"scored={e['data']['n_scored']}")
            # Old field (kept for backward compat)
            if "report_n_sources" in e["data"]:
                extra.append(f"final={e['data']['report_n_sources']}")
            # New fields (after FinalReport-attribute fix)
            if "report_n_ranked" in e["data"]:
                extra.append(f"ranked={e['data']['report_n_ranked']}")
            if "report_n_sources_total" in e["data"]:
                extra.append(f"total={e['data']['report_n_sources_total']}")
            if "report_n_api" in e["data"]:
                extra.append(
                    f"types=api:{e['data'].get('report_n_api',0)}/"
                    f"file:{e['data'].get('report_n_file',0)}/"
                    f"embedded:{e['data'].get('report_n_embedded',0)}"
                )
            start_ts = node_starts.get(node)
            offset = (start_ts - run_start_ts).total_seconds() if start_ts else 0
            print(f"│ [+{offset:6.1f}s] {node:22} {d_ms/1000:7.1f}s  {' '.join(extra)}")

    run_complete = next((e for e in events if e["event"] == "run_complete"), None)
    if run_complete:
        d = run_complete["data"]
        print(f"│")
        print(f"│ TOTAL: {d.get('total_duration_ms', 0)/1000:.1f}s  "
              f"cost=${d.get('cumulative_cost_usd', 0):.4f}")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 0: parse_intent output ──
    parse_intent_resp = next(
        (e for e in events
         if e["event"] == "llm_response" and e["data"].get("purpose") == "intent_parser"),
        None,
    )
    if parse_intent_resp:
        print(f"\n┌─ STAGE 0: parse_intent → StructuredRequirement ───────────────────────┐")
        resp = parse_intent_resp["data"].get("response", "")
        # Extract key fields from the response (Pydantic model __str__ format)
        for fld in ("domain=", "sub_domains=", "desired_formats=", "data_type_hints=",
                    "search_keywords_en=", "geographic_scope=", "target_registries=",
                    "known_authoritative_sources="):
            m = re.search(rf"{re.escape(fld)}(\[[^\]]*\]|'[^']*'|None)", resp)
            if m:
                val = m.group(1)
                # Truncate inline for display only
                if len(val) > 100:
                    val = val[:100] + "..."
                print(f"│  {fld[:-1]:30} = {val}")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 1: search_web queries + ALL URLs (now visible thanks to no-truncation) ──
    sw_calls = [e for e in events
                if e["event"] == "tool_call" and e["data"]["tool"] == "search_web"]
    sw_results = [e for e in events
                  if e["event"] == "tool_result" and e["data"]["tool"] == "search_web"]
    all_sw_urls: OrderedDict = OrderedDict()
    print(f"\n┌─ STAGE 1: search_web → candidate URLs ────────────────────────────────┐")
    for i, (call, res) in enumerate(zip(sw_calls, sw_results), 1):
        q = call["data"]["input"].get("query", "")
        summary = res["data"].get("summary", {})
        results_list = summary.get("results", []) or summary.get("urls_sample", [])
        print(f"│  [{i:2}] {q}")
        if isinstance(results_list, list):
            for r in results_list:
                url = r.get("url") if isinstance(r, dict) else r
                if url and url not in all_sw_urls:
                    all_sw_urls[url] = None
    print(f"│")
    print(f"│  Total unique URLs surfaced: {len(all_sw_urls)}")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 2: probe_url + fetch_page (agent's relevance gate) ──
    probed = OrderedDict()
    for e in events:
        if e["event"] == "tool_result" and e["data"]["tool"] == "probe_url":
            s = e["data"]["summary"]
            probed[s.get("url", "")] = (s.get("is_alive"), s.get("status_code"))

    fetched = OrderedDict()
    for e in events:
        if e["event"] == "tool_result" and e["data"]["tool"] == "fetch_page":
            s = e["data"]["summary"]
            url = s.get("url", "")
            fetched[url] = fetched.get(url, 0) + 1  # count duplicates

    print(f"\n┌─ STAGE 2: agent's relevance gate (probe + fetch) ─────────────────────┐")
    print(f"│  Funnel: {len(all_sw_urls)} search candidates → "
          f"{len(probed)} probed + {len(fetched)} fetched")
    print(f"│")
    if probed:
        print(f"│  probe_url ({len(probed)}):")
        for u, r in probed.items():
            alive, status = r
            mark = "✓" if alive else "✗"
            print(f"│    {mark} [{status}] {u[:80]}")
    if fetched:
        print(f"│  fetch_page ({sum(fetched.values())} calls, {len(fetched)} unique URLs):")
        for u, n in fetched.items():
            dup = f"  ⚠️ ×{n}" if n > 1 else ""
            print(f"│    [{n}] {u[:80]}{dup}")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 3: crawl_list_tree (if used) ──
    clt_calls = [e for e in events
                 if e["event"] == "tool_call" and e["data"]["tool"] == "crawl_list_tree"]
    crawl_nodes = [e for e in events if e["event"] == "crawl_node"]
    if clt_calls or crawl_nodes:
        print(f"\n┌─ STAGE 3: crawl_list_tree (recursive list crawl) ─────────────────────┐")
        for c in clt_calls:
            inp = c["data"]["input"]
            print(f"│  Seed URL:        {inp.get('url')}")
            print(f"│  max_total_pages: {inp.get('max_total_pages')}")
        print(f"│  Total crawl_node events: {len(crawl_nodes)}")
        for cn in crawl_nodes:
            d = cn["data"]
            print(f"│    depth={d['depth']} [{d['page_kind']:6}] "
                  f"links {d['links_total']}→{d['links_kept']}  {d['url'][:60]}")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 4: agent emits sources (parse from final_text now that it's logged) ──
    agent_done = next((e for e in events if e["event"] == "agent_done"), None)
    agent_sources: list[dict] = []
    if agent_done:
        ft = agent_done["data"].get("final_text", "")
        m = re.search(r"```(?:json)?\s*\n(\{.*?\})\n```", ft, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                agent_sources = parsed.get("sources", []) or []
            except Exception:
                pass
        # Also handle when final_text isn't logged (old runs)
        if not agent_sources:
            agent_node = next(
                (e for e in events
                 if e["event"] == "node_complete" and e["data"].get("node") == "agentic_discovery"),
                None,
            )
            if agent_node:
                print(f"\n┌─ STAGE 4: agentic_discovery emits sources ────────────────────────────┐")
                print(f"│  n_sources       = {agent_node['data'].get('n_sources')}")
                print(f"│  n_portal_trees  = {agent_node['data'].get('n_portal_trees')}")
                print(f"│  ⚠️ final_text not in log — pre-fix run")
                print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 4.5: portal_trees (DataPageTree) parsed from agent_done.final_text ──
    portal_trees: list[dict] = []
    if agent_done:
        ft = agent_done["data"].get("final_text", "")
        m = re.search(r"```(?:json)?\s*\n(\{.*?\})\n```", ft, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                portal_trees = parsed.get("portal_trees", []) or []
            except Exception:
                pass
    if portal_trees:
        print(f"\n┌─ STAGE 4 (cont): agent emits {len(portal_trees)} portal_tree(s) ─────────────────┐")

        def _render_node(n: dict, indent: int) -> None:
            pfx = "│  " + "  " * indent
            print(f"{pfx}[{n.get('page_kind') or n.get('page_type','?'):8}] depth={n.get('depth')} "
                  f"sampled={n.get('is_sampled')} fields={len(n.get('fields_available') or [])}")
            url = (n.get('url') or '')[:75]
            print(f"{pfx}    {url}")
            title = (n.get('title') or '')[:65]
            if title:
                print(f"{pfx}    \"{title}\"")
            for c in n.get('children') or []:
                _render_node(c, indent + 1)

        for i, tree in enumerate(portal_trees, 1):
            print(f"│  Tree {i}:")
            root = tree.get('root') or {}
            _render_node(root, 0)
            if (s := tree.get('tree_summary')):
                print(f"│    summary: {s[:200]}")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    if agent_sources:
        print(f"\n┌─ STAGE 4: agentic_discovery emits {len(agent_sources)} sources ─────────────────────┐")
        for i, s in enumerate(agent_sources, 1):
            name = s.get("name", "?")[:50]
            url = s.get("url", "?")[:60]
            stype = s.get("source_type", "?")
            print(f"│  [{i:2}] [{stype:8}] {name}")
            print(f"│       {url}")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 5: judge_stage_a (now URLs visible via full prompt) ──
    sa_calls = [e for e in events
                if e["event"] == "llm_call" and e["data"].get("purpose") == "judge_stage_a"]
    sa_urls: OrderedDict = OrderedDict()
    for c in sa_calls:
        # New format: messages list; pull all content + regex URLs
        msgs = c["data"].get("messages") or []
        content = " ".join(m.get("content", "") for m in msgs)
        # Fallback: prompt_excerpt + prompt_tail (intermediate format)
        if not content:
            content = c["data"].get("prompt_excerpt", "") + " " + c["data"].get("prompt_tail", "")
        for u in re.findall(r"https?://\S+", content):
            u = u.rstrip(".,;)\"\\")
            if u not in sa_urls:
                sa_urls[u] = None

    print(f"\n┌─ STAGE 5: judge_stage_a (Haiku batch relevance filter) ───────────────┐")
    print(f"│  {len(sa_calls)} LLM batch call(s), {len(sa_urls)} URLs evaluated:")
    for u in list(sa_urls.keys())[:20]:
        print(f"│    {u[:80]}")
    if len(sa_urls) > 20:
        print(f"│    ... and {len(sa_urls)-20} more")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 6: judge_stage_b (per-source scoring → URLs now visible too) ──
    sb_calls = [e for e in events
                if e["event"] == "llm_call" and e["data"].get("purpose") == "judge_stage_b"]
    sb_urls: list[str] = []
    sb_names: list[str] = []
    for c in sb_calls:
        msgs = c["data"].get("messages") or []
        content = " ".join(m.get("content", "") for m in msgs)
        if not content:
            content = c["data"].get("prompt_excerpt", "") + " " + c["data"].get("prompt_tail", "")
        m_url = re.search(r"-\s*URL:\s*(\S+)", content)
        m_name = re.search(r"-\s*Name:\s*([^\n]+)", content)
        if m_url:
            sb_urls.append(m_url.group(1).rstrip(".,;)\"\\"))
        if m_name:
            sb_names.append(m_name.group(1).strip())

    print(f"\n┌─ STAGE 6: judge_stage_b (Sonnet per-source 5-dim scoring) ────────────┐")
    print(f"│  {len(sb_calls)} LLM call(s):")
    for i, (n, u) in enumerate(zip(sb_names, sb_urls), 1):
        print(f"│  [{i:2}] {n[:50]}")
        print(f"│       {u[:80]}")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Stage 7: final report ──
    finalize_done = next(
        (e for e in events
         if e["event"] == "node_complete" and e["data"].get("node") == "finalize"),
        None,
    )
    print(f"\n┌─ STAGE 7: final report ───────────────────────────────────────────────┐")
    if finalize_done:
        d = finalize_done["data"]
        print(f"│  report_n_sources: {d.get('report_n_sources')}")
    print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── Skill proposals ──
    skills = [e for e in events
              if e["event"] == "tool_result" and e["data"]["tool"] == "propose_skill"]
    if skills:
        print(f"\n┌─ Skills proposed ({len(skills)}) ───────────────────────────────────────────┐")
        for s in skills:
            sm = s["data"]["summary"]
            print(f"│  {sm.get('etld1'):30} / {sm.get('pattern_id'):30} "
                  f"conf={sm.get('confidence')} types={sm.get('types')}")
        print(f"└──────────────────────────────────────────────────────────────────────┘")

    # ── FUNNEL SUMMARY ──
    print(f"\n┌─ FUNNEL SUMMARY ──────────────────────────────────────────────────────┐")
    print(f"│  Stage 1 search_web returned :  {len(all_sw_urls):4} unique URLs")
    print(f"│  Stage 2 agent picked to probe:  {len(probed):4} URLs (relevance gate)")
    print(f"│  Stage 2 agent picked to fetch:  {len(fetched):4} URLs")
    if agent_sources:
        print(f"│  Stage 4 agent emitted        :  {len(agent_sources):4} sources")
    print(f"│  Stage 5 stage_a evaluated    :  {len(sa_urls):4} URLs")
    print(f"│  Stage 6 stage_b scored       :  {len(sb_urls):4} sources")
    if finalize_done:
        rn = finalize_done["data"].get("report_n_sources")
        if rn is not None:
            print(f"│  Stage 7 final report         :  {rn:4} sources")
    print(f"└──────────────────────────────────────────────────────────────────────┘")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
