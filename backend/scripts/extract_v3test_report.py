"""Extract complete final report from v3test-5bb3d8 test artifacts."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SESSION_DIR = ROOT / "agent-workspace/agent-sessions/v3test-5bb3d8"
LOG = ROOT / "agent-workspace/run-logs/20260525T013829-v3test-5.log"
DIAG = ROOT / "agent-workspace/diagnostics/2026-05-25/v3test-5bb3d8.json"


def load_ranker_items():
    """Parse all _RankedItem entries from the ranker LLM response.

    Uses a forward-looking regex that delimits on rank=N to avoid single-quote
    breakage inside rationale text.
    """
    log = LOG.read_text(encoding="utf-8")
    # Find the judge_stage_b ranker response
    m = re.search(
        r'"purpose":\s*"judge_stage_b".*?"response":\s*"(.*?)(?<!\\)"\s*,\s*"prompt_tokens"',
        log, re.DOTALL,
    )
    if not m:
        return []
    text = m.group(1)

    items = []
    # Each item starts with _RankedItem(id=...) and ends with the next
    # _RankedItem( or end of list. Split by '_RankedItem(' as boundary.
    parts = text.split("_RankedItem(")
    for chunk in parts[1:]:
        # chunk now looks like: id='X', rank=Y, overall=Z, rationale='...', drop=Bool, drop_reason='...'),
        idm = re.match(r"id='([^']+)'", chunk)
        rm = re.search(r"rank=(\d+)", chunk)
        om = re.search(r"overall=([\d.]+)", chunk)
        dropm = re.search(r"drop=(True|False)", chunk)
        # Rationale: between rationale=' and ', \s*drop=
        ratm = re.search(r"rationale='(.*?)',\s*drop=", chunk, re.DOTALL)
        if not all([idm, rm, om, dropm]):
            continue
        items.append({
            "id": idm.group(1),
            "rank": int(rm.group(1)),
            "overall": float(om.group(1)),
            "rationale": (ratm.group(1) if ratm else "").replace("\\'", "'"),
            "drop": dropm.group(1) == "True",
        })
    return items


def load_scoring():
    """Pull Stage-A scoring LLM responses → id → (relevance, reason)."""
    log = LOG.read_text(encoding="utf-8")
    out = {}
    # Same split-trick for _ScoredItem
    parts = log.split("_ScoredItem(")
    for chunk in parts[1:]:
        idm = re.match(r"id='([^']+)'", chunk)
        relm = re.search(r"relevance=([\d.]+)", chunk)
        # reason uses double-quote in the log
        rsm = re.search(r'reason="(.*?)"\)', chunk, re.DOTALL)
        if not idm or not relm:
            continue
        sid = idm.group(1)
        if sid not in out:    # take first occurrence
            out[sid] = (
                float(relm.group(1)),
                (rsm.group(1) if rsm else "").replace('\\"', '"'),
            )
    return out


def load_sources():
    """sources.jsonl gives raw committed data, keyed by url."""
    out = {}
    with SESSION_DIR.joinpath("sources.jsonl").open(encoding="utf-8") as f:
        for line in f:
            s = json.loads(line)
            out[s["url"]] = s
    return out


def main():
    ranked = load_ranker_items()
    scoring = load_scoring()
    sources_by_url = load_sources()
    diag = json.loads(DIAG.read_text(encoding="utf-8"))

    print("=" * 80)
    print(f"Query:  {diag['user_query']}")
    print(f"Domain: {diag['domain']}")
    print(f"Cost:   ${diag['cost_usd']:.4f}  |  Total time: {sum(diag['stage_timings'].values()):.1f}s")
    print("=" * 80)
    print()

    print(f"=== Final Ranked Output ({len(ranked)} sources) ===")
    print()
    # Need to match ranker id to source. Since DataSource.id is a UUID
    # generated at construction time and NOT stored in sources.jsonl, we
    # match by ordering: ranker sees sources in commit order. Build that
    # mapping.
    src_list = list(sources_by_url.values())
    # Iterate ranker items sorted by rank
    for item in sorted(ranked, key=lambda x: x["rank"]):
        s_rel, s_reason = scoring.get(item["id"], (None, None))
        # Look up source by trying to find the one whose name appears in
        # the rationale (heuristic). Or just enumerate.
        # Use a more direct method: try to identify by matching the
        # rationale's first proper noun.
        match_url = None
        match_src = None
        for url, s in sources_by_url.items():
            if s["name"][:30] in item["rationale"][:200]:
                match_url = url
                match_src = s
                break

        flag = "✗ DROPPED" if item["drop"] else "✓"
        print(f"[#{item['rank']:>2}] overall={item['overall']:>4.1f}  {flag}")
        if match_src:
            print(f"      name:        {match_src['name']}")
            print(f"      url:         {match_src['url']}")
            print(f"      type:        {match_src['source_type']}")
            print(f"      provider:    {match_src.get('provider', '')}")
            print(f"      access:      {match_src.get('access_level', '?')}")
            print(f"      license:     {match_src.get('license', '?')}")
            tags = match_src.get('tags', [])
            if tags:
                print(f"      tags:        {', '.join(tags[:6])}")
            print(f"      description: {match_src['description'][:180]}")
        else:
            print(f"      (matched by id only: {item['id'][:16]})")
        if s_rel is not None:
            short_reason = (s_reason or '')[:160]
            print(f"      stage-a rel: {s_rel}  ({short_reason})")
        print(f"      ranker:      {item['rationale'][:240]}")
        print()


if __name__ == "__main__":
    main()
