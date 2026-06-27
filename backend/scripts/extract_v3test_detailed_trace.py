"""Per-source detailed trace through every judge stage for v3test-5bb3d8."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SESSION_DIR = ROOT / "agent-workspace/agent-sessions/v3test-5bb3d8"
LOG = ROOT / "agent-workspace/run-logs/20260525T013829-v3test-5.log"


def source_id(url: str, source_type: str) -> str:
    return hashlib.sha256(f"{url}:{source_type}".encode()).hexdigest()[:16]


def load_sources_by_id():
    """sources.jsonl → id-keyed dict."""
    out = {}
    with SESSION_DIR.joinpath("sources.jsonl").open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            s = json.loads(line)
            sid = source_id(s["url"], s["source_type"])
            s["_id"] = sid
            s["_commit_order"] = i
            out[sid] = s
    return out


def load_binary_decisions():
    """Map (batch_idx, index_in_batch) → keep + reason from binary LLM."""
    log = LOG.read_text(encoding="utf-8")
    # judge_stage_a LLM responses (fast tier batches)
    decisions = []  # list of [(batch_idx, item_idx, keep, reason), ...]
    batch_idx = 0
    for m in re.finditer(
        r'"purpose":\s*"judge_stage_a",.*?"response":\s*"(\[.*?\])"',
        log, re.DOTALL,
    ):
        raw = m.group(1).replace("\\n", "\n").replace('\\"', '"')
        # Strip surrounding quotes that are escaped
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            batch_idx += 1
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            decisions.append({
                "batch": batch_idx,
                "index": it.get("index"),
                "keep": it.get("keep", True),
                "reason": it.get("reason", ""),
            })
        batch_idx += 1
    return decisions


def load_scoring():
    log = LOG.read_text(encoding="utf-8")
    out = {}
    for chunk in log.split("_ScoredItem(")[1:]:
        idm = re.match(r"id='([^']+)'", chunk)
        relm = re.search(r"relevance=([\d.]+)", chunk)
        rsm = re.search(r'reason="(.*?)"\)', chunk, re.DOTALL)
        if not idm or not relm:
            continue
        sid = idm.group(1)
        if sid not in out:
            out[sid] = {
                "relevance": float(relm.group(1)),
                "reason": (rsm.group(1) if rsm else "").replace('\\"', '"'),
            }
    return out


def load_ranker():
    log = LOG.read_text(encoding="utf-8")
    items = {}
    m = re.search(
        r'"purpose":\s*"judge_stage_b".*?"response":\s*"(.*?)(?<!\\)"\s*,\s*"prompt_tokens"',
        log, re.DOTALL,
    )
    if not m:
        return items
    text = m.group(1)
    for chunk in text.split("_RankedItem(")[1:]:
        idm = re.match(r"id='([^']+)'", chunk)
        rm = re.search(r"rank=(\d+)", chunk)
        om = re.search(r"overall=([\d.]+)", chunk)
        dropm = re.search(r"drop=(True|False)", chunk)
        ratm = re.search(r"rationale='(.*?)',\s*drop=", chunk, re.DOTALL)
        if not all([idm, rm, om, dropm]):
            continue
        items[idm.group(1)] = {
            "rank": int(rm.group(1)),
            "overall": float(om.group(1)),
            "rationale": (ratm.group(1) if ratm else "").replace("\\'", "'"),
            "drop": dropm.group(1) == "True",
        }
    return items


def main():
    sources = load_sources_by_id()
    binary = load_binary_decisions()
    scoring = load_scoring()
    ranker = load_ranker()

    # Align binary decisions to commit order. Binary batch 1 = sources[0:10],
    # batch 2 = sources[10:18]. Binary `index` field is 1-based within batch.
    # Map (batch_idx * 10 + (index-1)) → commit_order.
    binary_by_commit = {}
    for d in binary:
        commit_order = d["batch"] * 10 + (d["index"] - 1)
        binary_by_commit[commit_order] = d

    # Map commit_order → source id
    commit_to_id = {s["_commit_order"]: sid for sid, s in sources.items()}

    print("=" * 90)
    print(f"Per-source trace through Stage-A + Stage-B  (test v3test-5bb3d8, 18 sources)")
    print(f"Query: 我需要可商用的北京酒店数据,要 JSON API,字段含酒店名、地址、价格")
    print("=" * 90)
    print()

    # Iterate by ranker rank
    sorted_by_rank = sorted(
        sources.values(),
        key=lambda s: ranker.get(s["_id"], {"rank": 999})["rank"],
    )

    for src in sorted_by_rank:
        sid = src["_id"]
        rk = ranker.get(sid, {})
        sc = scoring.get(sid, {})
        bn = binary_by_commit.get(src["_commit_order"], {})

        rank = rk.get("rank", "?")
        print(f"━━━ Rank #{rank} ━━━ id={sid}  commit_order={src['_commit_order']}")
        print(f"  Name:        {src['name']}")
        print(f"  URL:         {src['url']}")
        print(f"  Type:        {src['source_type']}")
        print(f"  Provider:    {src.get('provider', '')}")
        print(f"  License:     {src.get('license', '?')}")
        print(f"  Access:      {src.get('access_level', '?')}")
        print(f"  Description: {src['description'][:180]}")
        print()
        print(f"  Stage-A Binary Filter (fast LLM):")
        if bn:
            kept = bn.get('keep')
            print(f"    decision: {'✓ KEEP' if kept else '✗ DROP'}")
            print(f"    reason:   {bn.get('reason', '')}")
        else:
            print(f"    (no decision logged — possibly llm_prior bypass / small-pool bypass)")
        print()
        print(f"  Stage-A Probe (HTTP HEAD/GET):")
        print(f"    decision: ✓ ALIVE  (implied — reached scoring step)")
        print()
        print(f"  Stage-A Scoring (strong LLM):")
        if sc:
            print(f"    relevance: {sc['relevance']}")
            print(f"    reason:    {sc['reason'][:300]}")
        else:
            print(f"    (no scoring data)")
        print()
        print(f"  Stage-B Dim Scoring (deterministic, no LLM):")
        print(f"    [authority / accessibility / freshness / license_fit / "
              f"format_fit / temporal_fit / geographic_fit / schema_coverage]")
        print(f"    Not captured in run-log (DEBUG-level only); rationale inferred")
        print(f"    from ranker's analysis below.")
        print()
        print(f"  Stage-B LLM Ranker (strong LLM, one call sees all 18):")
        if rk:
            print(f"    rank:      #{rk['rank']}  (out of 18)")
            print(f"    overall:   {rk['overall']}")
            print(f"    drop:      {'YES' if rk['drop'] else 'NO'}")
            print(f"    rationale: {rk['rationale'][:300]}")
        else:
            print(f"    (no ranker data)")
        print()
        print()


if __name__ == "__main__":
    main()
