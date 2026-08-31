"""Phase 0 — normalize the domain-skills library (DESTRUCTIVE, gated).

Policy (per user decision 2026-06-04 "全部删掉"):
  * DELETE every pattern with a dead regex (full-URL anchored → never matches
    the path-only `lookup(etld1, url_path)` input).
  * DELETE every pattern with empty `types:` (ambiguous / forgotten).
  * STRIP `confidence` + the self-correction counters from survivors
    (no-confidence model: trust by default, fix on error).
  * Remove a domain's classify.md (and dir) if it ends with 0 patterns.

The library re-grows correct entries via the closed loop; we don't salvage
broken patterns here.

Safety:
  * Default is DRY-RUN — prints exactly what WOULD change, writes nothing.
  * `--apply` first copies the whole domain-skills/ tree to a timestamped
    backup directory, THEN rewrites in place. The backup parent defaults to
    `<repo>/.skill-backups/` and is overridable with `--backup-parent` —
    point it at another disk if this one is tight.
  * Block-level surgery: deleted patterns drop their whole `## Pattern` block;
    survivors keep notes/regex/types/evidence_count byte-for-byte minus the
    stripped lines. The frontmatter + intro are preserved verbatim.

Self-contained: stdlib only.

    python backend/scripts/normalize_skills.py            # dry-run
    python backend/scripts/normalize_skills.py --apply    # backup + write
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "agent-workspace" / "domain-skills"
# In-repo and gitignored, so it works on any machine. Override with
# --backup-parent to put the copy on another disk.
DEFAULT_BACKUP_PARENT = Path(__file__).resolve().parent.parent.parent / ".skill-backups"

_PATTERN_HEADER_RE = re.compile(r"^##\s+Pattern:\s*(.+?)\s*$", re.MULTILINE)
_STRIP_FIELDS = ("confidence", "success_count", "failure_count", "consecutive_failures")
_STRIP_LINE_RE = re.compile(rf"^(?:{'|'.join(_STRIP_FIELDS)})\s*:.*$\n?", re.MULTILINE)


def _is_dead_regex(regex: str) -> bool:
    r = regex.strip()
    return "://" in r or bool(re.match(r"^\^?\(?https?", r))


def _seg_field(seg_text: str, key: str) -> str:
    m = re.search(rf"^{key}\s*:(.*)$", seg_text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _split(text: str) -> tuple[str, list[dict]]:
    """Return (head, segments). head = frontmatter + intro before first pattern.
    Each segment = {id, regex, types, text} where text is the RAW block incl. its
    `## Pattern:` header through to (excluding) the next header."""
    headers = list(_PATTERN_HEADER_RE.finditer(text))
    if not headers:
        return text, []
    head = text[: headers[0].start()]
    segs = []
    for i, h in enumerate(headers):
        start = h.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        segs.append(
            {
                "id": h.group(1).strip(),
                "regex": _seg_field(block, "regex"),
                "types": _seg_field(block, "types"),
                "text": block,
            }
        )
    return head, segs


def _plan_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    head, segs = _split(text)
    deleted, kept = [], []
    for s in segs:
        reason = None
        if not s["regex"] or _is_dead_regex(s["regex"]):
            reason = "dead-regex" if s["regex"] else "no-regex"
        elif not s["types"]:
            reason = "empty-types"
        if reason:
            deleted.append((s["id"], reason))
        else:
            kept.append(s)

    # Rebuild survivors with stripped fields.
    new_segs = [_STRIP_LINE_RE.sub("", s["text"]) for s in kept]
    conf_stripped = sum(len(_STRIP_LINE_RE.findall(s["text"])) for s in kept)
    new_text = head + "".join(new_segs)
    new_text = new_text.rstrip() + "\n" if new_text.strip() else ""
    return {
        "domain": path.parent.name,
        "path": path,
        "n_before": len(segs),
        "n_after": len(kept),
        "deleted": deleted,
        "conf_stripped": conf_stripped,
        "new_text": new_text,
        "remove_domain": len(kept) == 0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Normalize (purge broken) the domain-skills library.")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument(
        "--backup-parent",
        default=str(DEFAULT_BACKUP_PARENT),
        help=f"where --apply writes its pre-write copy (default: {DEFAULT_BACKUP_PARENT})",
    )
    ap.add_argument("--apply", action="store_true", help="back up + write (default is dry-run)")
    ap.add_argument(
        "--no-backup", action="store_true", help="skip the safety backup (irreversible)"
    )
    args = ap.parse_args(argv)

    root = Path(args.root)
    files = sorted(root.glob("*/classify.md"))
    if not files:
        print(f"[ERR] no classify.md under {root}", file=sys.stderr)
        return 1

    plans = [_plan_file(f) for f in files]
    tot_del = sum(len(p["deleted"]) for p in plans)
    tot_strip = sum(p["conf_stripped"] for p in plans)
    rm_domains = [p["domain"] for p in plans if p["remove_domain"]]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] root: {root}\n")
    for p in plans:
        if not p["deleted"] and p["conf_stripped"] == 0:
            continue
        flag = "  → REMOVE (0 patterns left)" if p["remove_domain"] else ""
        print(
            f"{p['domain']:<26} {p['n_before']}→{p['n_after']} patterns, "
            f"conf-stripped={p['conf_stripped']}{flag}"
        )
        for pid, reason in p["deleted"]:
            print(f"    - delete [{reason}] {pid}")

    print("\n" + "=" * 60)
    print(f"patterns deleted        : {tot_del}")
    print(f"confidence/counters cut : {tot_strip} lines")
    print(f"domains removed (empty) : {len(rm_domains)}  {rm_domains}")
    print(
        f"domains touched         : {sum(1 for p in plans if p['deleted'] or p['conf_stripped'])}"
    )

    if not args.apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply to back up + write.")
        return 0

    # Apply: back up the whole tree first (unless --no-backup).
    backup = None
    if not args.no_backup:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_parent = Path(args.backup_parent)
        backup = backup_parent / f"domain-skills-{ts}"
        backup_parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, backup)
        print(f"\n[backup] {root}  →  {backup}")
    else:
        print("\n[no-backup] skipping backup (irreversible)")

    for p in plans:
        if not (p["deleted"] or p["conf_stripped"]):
            continue
        if p["remove_domain"]:
            shutil.rmtree(p["path"].parent)
            print(f"[removed] {p['path'].parent}")
        else:
            p["path"].write_text(p["new_text"], encoding="utf-8")
            print(f"[wrote]   {p['path']}")
    print("\n[done]" + (f" backup at {backup}" if backup else " (no backup taken)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
