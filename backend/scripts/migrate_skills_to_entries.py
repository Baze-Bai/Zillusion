"""Phase 1b — migrate the domain-skills library from the old FLAT markdown
schema (regex + types + confidence + notes) to the new ENTRY schema
(regex index → described-URL entries), written as classify.yaml.

Each old flat pattern becomes ONE pattern with ONE entry:
  * types  → entry.types
  * notes  → entry.notes (prose preserved verbatim)
  * url + structured fields (page_type/data_type/site_type/fields/caveats)
    start EMPTY — they populate going forward via new proposals / harness
    learning.

Writes ``classify.yaml`` next to each ``classify.md`` and removes the .md.

    python backend/scripts/migrate_skills_to_entries.py            # dry-run
    python backend/scripts/migrate_skills_to_entries.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Use the NEW models/serializer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.services.skill_library import (  # noqa: E402
    CLASSIFY_FILENAME,
    DomainSkillFile,
    Skill,
    SkillEntry,
    _types_from_names,
    serialize_skill_file,
)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "agent-workspace" / "domain-skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)
_PATTERN_HEADER_RE = re.compile(r"^##\s+Pattern:\s*(.+?)\s*$", re.MULTILINE)


def _parse_kv_block(block: str) -> dict[str, str]:
    """Parse a pattern body's ``key: value`` lines, honoring a ``notes: |``
    literal block (indented continuation lines)."""
    out: dict[str, str] = {}
    current_key: str | None = None
    cont: list[str] = []

    def _flush() -> None:
        nonlocal current_key, cont
        if current_key is not None and cont:
            out[current_key] = (out.get(current_key, "") + "\n".join(cont)).strip()
            cont = []

    for raw in block.splitlines():
        line = raw.rstrip()
        if line.startswith("  ") and current_key is not None:
            cont.append(line.strip())
            continue
        if not line:
            continue
        _flush()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if val == "|":
            current_key = key
            out.setdefault(key, "")
            cont = []
        else:
            out[key] = val
            current_key = key
    _flush()
    return out


def _flat_to_domainfile(text: str, domain: str) -> DomainSkillFile:
    fm = _FRONTMATTER_RE.match(text)
    body = text[fm.end() :] if fm else text
    dom = domain
    if fm:
        for line in fm.group(1).splitlines():
            if line.strip().lower().startswith("domain:"):
                dom = line.split(":", 1)[1].strip() or domain

    skills: list[Skill] = []
    headers = list(_PATTERN_HEADER_RE.finditer(body))
    for i, h in enumerate(headers):
        block = body[h.end() : headers[i + 1].start() if i + 1 < len(headers) else len(body)]
        kv = _parse_kv_block(block)
        regex = (kv.get("regex") or "").strip()
        if not regex:
            continue
        types = _types_from_names((kv.get("types") or "").split(","))
        skills.append(
            Skill(
                pattern_id=h.group(1).strip(),
                regex=regex,
                entries=[SkillEntry(types=types, notes=(kv.get("notes") or "").strip())],
            )
        )
    return DomainSkillFile(domain=dom, skills=skills)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Migrate domain-skills flat .md → entry .yaml.")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument(
        "--apply", action="store_true", help="write .yaml + remove .md (default dry-run)"
    )
    args = ap.parse_args(argv)

    root = Path(args.root)
    md_files = sorted(root.glob("*/classify.md"))
    if not md_files:
        print(f"[ERR] no classify.md under {root} (already migrated?)", file=sys.stderr)
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(md_files)} domain files\n")
    tot_patterns = tot_entries = 0
    for md in md_files:
        domain = md.parent.name
        dsf = _flat_to_domainfile(md.read_text(encoding="utf-8", errors="replace"), domain)
        n_p = len(dsf.skills)
        n_e = sum(len(s.entries) for s in dsf.skills)
        tot_patterns += n_p
        tot_entries += n_e
        print(f"  {domain:<26} {n_p} pattern(s) → {n_e} entr(y/ies)")
        if args.apply:
            yaml_path = md.parent / CLASSIFY_FILENAME
            yaml_path.write_text(serialize_skill_file(dsf), encoding="utf-8")
            md.unlink()

    print(f"\ntotal: {len(md_files)} domains, {tot_patterns} patterns, {tot_entries} entries")
    if not args.apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply.")
    else:
        print("\n[done] wrote classify.yaml + removed classify.md per domain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
