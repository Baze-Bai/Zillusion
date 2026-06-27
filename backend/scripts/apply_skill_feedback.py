"""Phase 4 — apply harness feedback to the skill library (backend consumer).

Reads a ``skill_feedback.json`` emitted by the discovery→harness bridge after
harness runs, and corrects the skill library. Two independent lines:

  TYPE (deterministic): for an item carrying a ``real_type`` (the type the
  harness actually built/validated), re-match its URL against the library.
    * a matching pattern whose focused entry does NOT already include
      real_type → update that entry's types to [real_type].
    * NO matching pattern → propose a NEW skill (conservative: exact-path
      regex, filed under the host; consolidate_skills generalizes later).

  RELEVANCE (LLM reviewer): for an item flagged ``relevance="irrelevant"``
  with a ``reason`` + a ``fresh_description`` (the harness explore agent's
  same-format description of what the source actually is), re-match the URL
  and ask an LLM to compare the OLD entry vs the fresh one. It returns
  {no_change | update_description | delete}. GUARDRAIL: relevance is
  query-dependent — only modify when the fresh description shows the OLD
  description was INACCURATE; pure off-topic-for-this-query → no_change
  (that is a source-selection problem, not a skill problem).

Re-match uses a HOST-SUFFIX SCAN (find existing skill domains that are a
suffix of the URL host, then match path regexes) — robust to however the
agent computed etld1, and to subdomain-filed skills.

skill_feedback.json schema — a list of items:
  [
    {"url": "https://...",
     "real_type": "embedded"|"file"|"api"|"none"|null,
     "relevance": "irrelevant"|null,
     "reason": "...",                         # required when relevance set
     "fresh_description": {                    # harness explore's entry-format obs
        "types": ["file"], "page_type": "download", "data_type": "...",
        "site_type": "...", "fields": [...], "caveats": "...", "notes": "..."}}
  ]

    python backend/scripts/apply_skill_feedback.py inputs/skill_feedback.json            # dry-run
    python backend/scripts/apply_skill_feedback.py inputs/skill_feedback.json --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.models.data_source import DataSourceType  # noqa: E402
from src.services.skill_library import (  # noqa: E402
    CLASSIFY_FILENAME,
    Skill,
    SkillEntry,
    get_skill_library,
)

_TYPE_BY_NAME = {
    "api": DataSourceType.API,
    "file": DataSourceType.DOWNLOADABLE_FILE,
    "embedded": DataSourceType.EMBEDDED_DATA,
}
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _host(url: str) -> str:
    h = (urlparse(url).netloc or "").lower()
    return h[4:] if h.startswith("www.") else h


def _url_path(url: str) -> str:
    p = urlparse(url)
    path = p.path or "/"
    return f"{path}?{p.query}" if p.query else path


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-")[:60] or "pattern"


def _all_skill_domains(root: Path) -> list[str]:
    return sorted(p.parent.name for p in root.glob(f"*/{CLASSIFY_FILENAME}"))


async def _rematch(lib, url: str, all_domains: list[str]) -> list[tuple[str, Skill]]:
    """Find (domain, pattern) pairs where a skill regex matches this URL. A
    candidate domain is one that equals the host or is a dotted suffix of it
    (so a subdomain URL matches both the bare and the subdomain-filed skill).
    Sorted most-specific-domain first."""
    host, path = _host(url), _url_path(url)
    cands = [d for d in all_domains if host == d or host.endswith("." + d)]
    cands.sort(key=len, reverse=True)
    out: list[tuple[str, Skill]] = []
    for d in cands:
        dsf = await lib.load(d)
        out.extend((d, s) for s in dsf.skills if s.matches(path))
    return out


# ── relevance LLM reviewer ──────────────────────────────────────────────


async def _review_relevance(old_entry: SkillEntry, fresh: dict, reason: str):
    """Return ('no_change'|'update_description'|'delete', patch_dict)."""
    from pydantic import BaseModel, Field

    from src.services.llm import llm_service

    class _Verdict(BaseModel):
        decision: str = Field(description="no_change | update_description | delete")
        page_type: str = ""
        data_type: str = ""
        site_type: str = ""
        fields: list[str] = Field(default_factory=list)
        caveats: str = ""
        notes: str = ""

    old = {
        "types": [t.value for t in old_entry.types],
        "page_type": old_entry.page_type,
        "data_type": old_entry.data_type,
        "site_type": old_entry.site_type,
        "fields": old_entry.fields,
        "caveats": old_entry.caveats,
        "notes": old_entry.notes,
    }
    prompt = (
        "A downstream crawler explored a source the discovery agent had classified "
        "and reported it as NOT relevant to the user's query.\n\n"
        f"Irrelevance reason: {reason}\n\n"
        f"OLD skill description (what we recorded before):\n{json.dumps(old, ensure_ascii=False, indent=2)}\n\n"
        f"FRESH first-hand description (what the page ACTUALLY is):\n{json.dumps(fresh, ensure_ascii=False, indent=2)}\n\n"
        "Decide:\n"
        "- `update_description`: the FRESH description shows the OLD one is INACCURATE "
        "(wrong fields/caveats/data_type). Return the corrected fields.\n"
        "- `delete`: the OLD pattern is fundamentally wrong about what this URL shape is.\n"
        "- `no_change`: the OLD description is ACCURATE and the source is merely "
        "off-topic for THIS query (a source-selection issue, NOT a skill error) — "
        "this is the DEFAULT; relevance is query-dependent, do not corrupt an accurate skill.\n"
    )
    try:
        v, _ = await llm_service.complete_structured(
            messages=[{"role": "user", "content": prompt}],
            response_model=_Verdict,
            profile="type_classifier",
            model_tier="fast",
        )
    except Exception:  # noqa: BLE001
        return "no_change", {}  # fail safe — never corrupt on LLM failure
    if v.decision == "update_description":
        patch = {
            k: getattr(v, k)
            for k in ("page_type", "data_type", "site_type", "fields", "caveats", "notes")
            if getattr(v, k)
        }
        return "update_description", patch
    if v.decision == "delete":
        return "delete", {}
    return "no_change", {}


# ── main ────────────────────────────────────────────────────────────────


async def _run(items: list[dict], root: Path, apply: bool) -> dict:
    lib = get_skill_library()
    all_domains = _all_skill_domains(root)
    stats = {
        "type_fixed": 0,
        "type_new": 0,
        "rel_updated": 0,
        "rel_deleted": 0,
        "rel_no_change": 0,
        "no_skill": 0,
        "agree": 0,
    }
    log: list[str] = []

    for it in items:
        url = (it.get("url") or "").strip()
        if not url:
            continue
        path = _url_path(url)
        matches = await _rematch(lib, url, all_domains)

        # ── TYPE line ──
        rt = (it.get("real_type") or "").strip().lower()
        rt_enum = _TYPE_BY_NAME.get(rt)
        if rt:
            if matches:
                for d, s in matches:
                    e = s.focused_entry(path)
                    cur = e.types if e else []
                    if rt_enum is None:  # 'none' → harness found it's not a data source
                        if cur:
                            log.append(
                                f"TYPE  {d}/{s.pattern_id}: {[t.value for t in cur]} → [] (none) for {path}"
                            )
                            if apply:
                                await lib.update(d, s.pattern_id, url=path, types=[])
                            stats["type_fixed"] += 1
                        else:
                            stats["agree"] += 1
                    elif rt_enum not in cur:
                        log.append(
                            f"TYPE  {d}/{s.pattern_id}: {[t.value for t in cur]} → [{rt}] for {path}"
                        )
                        if apply:
                            await lib.update(d, s.pattern_id, url=path, types=[rt_enum])
                        stats["type_fixed"] += 1
                    else:
                        stats["agree"] += 1
            elif rt_enum is not None:
                # learn a NEW conservative skill (exact-path regex; consolidate later)
                etld1 = _host(url)
                pid = _slug(path.lstrip("/").split("?")[0]) or "shape"
                regex = "^" + re.escape(path) + "$"
                log.append(f"TYPE  +new {etld1}/{pid}: [{rt}] regex={regex}")
                if apply:
                    await lib.propose(
                        etld1,
                        Skill(
                            pattern_id=pid,
                            regex=regex,
                            entries=[
                                SkillEntry(
                                    url=path,
                                    types=[rt_enum],
                                    notes="learned from harness validation",
                                )
                            ],
                        ),
                    )
                stats["type_new"] += 1

        # ── RELEVANCE line ──
        if (it.get("relevance") or "").strip().lower() == "irrelevant":
            reason = (it.get("reason") or "").strip()
            fresh = it.get("fresh_description") or {}
            if not matches:
                stats["no_skill"] += 1
                log.append(f"REL   no skill for {url} — source-selection signal only")
            elif reason:
                d, s = matches[0]
                e = s.focused_entry(path) or SkillEntry()
                decision, patch = await _review_relevance(e, fresh, reason)
                log.append(f"REL   {d}/{s.pattern_id}: {decision} for {path}")
                if decision == "update_description":
                    if apply and patch:
                        await lib.update(d, s.pattern_id, url=path, entry_patch=patch)
                    stats["rel_updated"] += 1
                elif decision == "delete":
                    if apply:
                        await lib.delete(d, s.pattern_id, reason="harness: irrelevant + inaccurate")
                    stats["rel_deleted"] += 1
                else:
                    stats["rel_no_change"] += 1

    if apply:
        await lib.flush()
    return {"stats": stats, "log": log}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Apply harness skill_feedback.json to the skill library."
    )
    ap.add_argument("feedback", help="path to skill_feedback.json")
    ap.add_argument("--root", default=None, help="domain-skills/ dir (default: settings)")
    ap.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    args = ap.parse_args(argv)

    items = json.loads(Path(args.feedback).read_text(encoding="utf-8"))
    if isinstance(items, dict):
        items = items.get("items") or items.get("feedback") or []
    if args.root:
        root = Path(args.root)
    else:
        from src.config import settings

        root = Path(settings.skills.workspace_dir)

    out = asyncio.run(_run(items, root, args.apply))
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(items)} feedback item(s)\n")
    for line in out["log"]:
        print("  " + line)
    print("\n" + json.dumps(out["stats"], indent=2))
    if not args.apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
