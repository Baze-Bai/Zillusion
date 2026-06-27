"""Phase 0 — read-only audit of the domain-skills library.

Scans every `agent-workspace/domain-skills/<domain>/classify.md` and reports,
WITHOUT mutating anything:

  * dead regex  — anchored to a full URL (scheme/host), so it can NEVER match
                  the path-only string `lookup(etld1, url_path)` feeds in.
  * invalid     — regex doesn't compile.
  * no-types    — `types:` empty (ambiguous: skip-skill vs forgotten).
  * has-conf    — carries a `confidence` (slated for removal under the
                  no-confidence model).
  * dup-ish     — within a domain, regexes that are identical or where one is
                  a substring of another (overlap candidates for consolidate).

Self-contained: stdlib only, no backend imports. Run with any python:

    python backend/scripts/audit_skills.py
    python backend/scripts/audit_skills.py --list-dead     # also print each dead regex
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# domain-skills/ lives at  <backend>/agent-workspace/domain-skills/
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "agent-workspace" / "domain-skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)
_PATTERN_HEADER_RE = re.compile(r"^##\s+Pattern:\s*(.+?)\s*$", re.MULTILINE)


def _parse_patterns(text: str) -> list[dict]:
    """Minimal parser mirroring skill_library.parse_skill_file, but dependency
    free. Returns a list of {pattern_id, regex, types, confidence}."""
    m = _FRONTMATTER_RE.match(text)
    body = text[m.end() :] if m else text

    out: list[dict] = []
    headers = list(_PATTERN_HEADER_RE.finditer(body))
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        block = body[start:end]
        fields: dict[str, str] = {}
        for line in block.splitlines():
            line = line.rstrip()
            if line.startswith("  "):  # literal-block continuation — ignore for audit
                continue
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
        out.append(
            {
                "pattern_id": h.group(1).strip(),
                "regex": fields.get("regex", ""),
                "types": fields.get("types", ""),
                "confidence": fields.get("confidence"),
            }
        )
    return out


def _is_dead_regex(regex: str) -> bool:
    """A regex is 'dead' for path-only matching when it requires a scheme/host
    prefix — `lookup` feeds it `parsed.path (+?query)`, which never starts with
    `http`/`://`. Catches the noaa/geofabrik-style `^https?://host/...` skills."""
    r = regex.strip()
    if "://" in r:
        return True
    if re.match(r"^\^?\(?https?", r):
        return True
    return False


def _regex_invalid(regex: str) -> bool:
    try:
        re.compile(regex)
        return False
    except re.error:
        return True


def _domain_dups(patterns: list[dict]) -> list[tuple[str, str]]:
    """Return (a, b) pattern_id pairs whose regexes are identical or where one
    regex string contains the other (cheap overlap heuristic — real merge is
    consolidate_skills' job)."""
    pairs = []
    for i in range(len(patterns)):
        for j in range(i + 1, len(patterns)):
            ra, rb = patterns[i]["regex"].strip(), patterns[j]["regex"].strip()
            if not ra or not rb:
                continue
            if ra == rb or ra in rb or rb in ra:
                pairs.append((patterns[i]["pattern_id"], patterns[j]["pattern_id"]))
    return pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only audit of the domain-skills library.")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="domain-skills/ directory")
    ap.add_argument("--list-dead", action="store_true", help="print every dead regex")
    ap.add_argument("--list-notypes", action="store_true", help="print every empty-types pattern")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"[ERR] not found: {root}", file=sys.stderr)
        return 1

    files = sorted(root.glob("*/classify.md"))
    totals = Counter()
    dead_list: list[str] = []
    notypes_list: list[str] = []
    dup_domains: list[str] = []

    print(f"domain-skills root: {root}")
    print(f"domains: {len(files)}\n")
    print(
        f"{'domain':<26} {'pats':>4} {'dead':>4} {'inval':>5} {'noType':>6} {'conf':>4} {'dupPairs':>8}"
    )
    print("-" * 64)

    for f in files:
        domain = f.parent.name
        pats = _parse_patterns(f.read_text(encoding="utf-8", errors="replace"))
        dead = inval = notype = conf = 0
        for p in pats:
            rgx = p["regex"]
            if not rgx:
                inval += 1  # missing regex counts as invalid for our purposes
            elif _regex_invalid(rgx):
                inval += 1
            elif _is_dead_regex(rgx):
                dead += 1
                dead_list.append(f"  {domain:<24} {p['pattern_id']:<26} {rgx}")
            if not p["types"].strip():
                notype += 1
                notypes_list.append(f"  {domain:<24} {p['pattern_id']:<26} (regex={rgx})")
            if p["confidence"] is not None:
                conf += 1
        dups = _domain_dups(pats)
        if dups:
            dup_domains.append(f"  {domain}: " + ", ".join(f"{a}~{b}" for a, b in dups))

        totals["patterns"] += len(pats)
        totals["dead"] += dead
        totals["invalid"] += inval
        totals["notype"] += notype
        totals["conf"] += conf
        totals["dup_pairs"] += len(dups)
        totals["domains"] += 1

        print(
            f"{domain:<26} {len(pats):>4} {dead:>4} {inval:>5} {notype:>6} {conf:>4} {len(dups):>8}"
        )

    print("-" * 64)
    print(
        f"{'TOTAL':<26} {totals['patterns']:>4} {totals['dead']:>4} "
        f"{totals['invalid']:>5} {totals['notype']:>6} {totals['conf']:>4} {totals['dup_pairs']:>8}"
    )

    p = totals["patterns"] or 1
    print(f"\nSummary across {totals['domains']} domains / {totals['patterns']} patterns:")
    print(
        f"  dead regex (full-URL anchored, never match path) : {totals['dead']:>3}  ({100 * totals['dead'] // p}%)"
    )
    print(
        f"  invalid / missing regex                          : {totals['invalid']:>3}  ({100 * totals['invalid'] // p}%)"
    )
    print(
        f"  empty types (ambiguous skip vs forgotten)        : {totals['notype']:>3}  ({100 * totals['notype'] // p}%)"
    )
    print(
        f"  carries confidence (to strip)                    : {totals['conf']:>3}  ({100 * totals['conf'] // p}%)"
    )
    print(f"  within-domain overlap pairs (consolidate cand.)  : {totals['dup_pairs']:>3}")

    if args.list_dead and dead_list:
        print("\nDEAD regexes:")
        print("\n".join(dead_list))
    if args.list_notypes and notypes_list:
        print("\nEMPTY-types patterns:")
        print("\n".join(notypes_list))
    if dup_domains:
        print("\nDomains with overlapping regexes:")
        print("\n".join(dup_domains))

    return 0


if __name__ == "__main__":
    sys.exit(main())
