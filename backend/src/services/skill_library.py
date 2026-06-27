r"""Filesystem-backed Skill Library — regex index → described-URL entries.

Model (locked 2026-06-04): a skill is a **pure-index regex** pointing at a
bucket of **described URL entries**. The regex carries NO type/description —
all qualitative content lives on each entry. There is NO confidence and NO
success/failure counters: a skill is trusted by default and fixed/deleted on
error (the closed loop: in-run self-correction + harness feedback).

Storage (one YAML file per eTLD+1):

    domain-skills/<etld1>/classify.yaml

    domain: ctrip.com
    last_updated: 2026-06-04T...
    patterns:
      - pattern_id: beijing-hotel-list
        regex: ^/hotel/beijing\d+$          # PURE INDEX — no type/desc here
        entries:
          - url: /hotel/beijing1
            types: [embedded]               # [] = not a data source / skip
            page_type: list                 # list/detail/search/download/doc/other
            data_type: hotel-listing
            site_type: travel-OTA
            fields: [hotel_name, star_rating, price, district]
            caveats: "SPA; price requires login"
            notes: "..."

Lookup matches a URL's path against the regexes and returns the FOCUSED entry
(exact-url match if present, else the first/representative one) — not the
whole bucket.

Concurrency: per-domain asyncio.Lock serializes writes; atomic temp-file
rename so partial writes never corrupt a file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.models.data_source import DataSourceType

logger = logging.getLogger(__name__)

CLASSIFY_FILENAME = "classify.yaml"

# Canonical string names used in the YAML (stable, independent of enum .value).
_TYPE_BY_NAME: dict[str, DataSourceType] = {
    "api": DataSourceType.API,
    "file": DataSourceType.DOWNLOADABLE_FILE,
    "embedded": DataSourceType.EMBEDDED_DATA,
}
_NAME_BY_TYPE: dict[DataSourceType, str] = {v: k for k, v in _TYPE_BY_NAME.items()}


def _types_from_names(names: Any) -> list[DataSourceType]:
    out: list[DataSourceType] = []
    for raw in names or []:
        t = _TYPE_BY_NAME.get(str(raw).strip().lower())
        if t and t not in out:
            out.append(t)
    return out


def _type_names(types: list[DataSourceType]) -> list[str]:
    return [_NAME_BY_TYPE[t] for t in types if t in _NAME_BY_TYPE]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ──────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────


class SkillEntry(BaseModel):
    """One described URL under a pattern's regex. All qualitative content
    (type, page/data/site characterization, fields, caveats) lives here —
    NOT on the regex."""

    model_config = ConfigDict(extra="ignore")

    url: str = ""
    types: list[DataSourceType] = Field(default_factory=list)  # [] = not a data source / skip
    page_type: str = ""  # list / detail / search / download / doc / other
    data_type: str = ""  # domain-term kind, e.g. "hotel-listing"
    site_type: str = ""  # publisher nature, e.g. "travel-OTA"
    fields: list[str] = Field(default_factory=list)
    caveats: str = ""
    notes: str = ""


class Skill(BaseModel):
    """A pattern: a pure-index regex + a bucket of described URL entries."""

    model_config = ConfigDict(extra="ignore")

    pattern_id: str
    regex: str
    entries: list[SkillEntry] = Field(default_factory=list)

    def matches(self, url_path: str) -> bool:
        try:
            return bool(re.search(self.regex, url_path))
        except re.error:
            logger.warning("Skill %s has invalid regex %r; skipping", self.pattern_id, self.regex)
            return False

    def focused_entry(self, url_path: str) -> SkillEntry | None:
        """The entry relevant to this URL: exact-url match if present, else the
        first (representative) entry. None if the bucket is empty."""
        if not self.entries:
            return None
        exact = next((e for e in self.entries if e.url and e.url == url_path), None)
        return exact or self.entries[0]


class DomainSkillFile(BaseModel):
    """All patterns for one eTLD+1 domain."""

    model_config = ConfigDict(extra="ignore")

    domain: str
    last_updated: str | None = None
    skills: list[Skill] = Field(default_factory=list)  # attr kept as `skills` (== patterns)


# ──────────────────────────────────────────────────────────────────────
# Parser / serializer (YAML)
# ──────────────────────────────────────────────────────────────────────


def parse_skill_file(text: str, domain: str) -> DomainSkillFile:
    """Parse a domain classify.yaml into a DomainSkillFile."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.warning("skill_library.parse_failed: domain=%s %s", domain, exc)
        return DomainSkillFile(domain=domain)
    if not isinstance(data, dict):
        return DomainSkillFile(domain=domain)

    skills: list[Skill] = []
    for p in data.get("patterns") or []:
        if not isinstance(p, dict):
            continue
        regex = str(p.get("regex") or "").strip()
        if not regex:
            continue
        entries: list[SkillEntry] = []
        for e in p.get("entries") or []:
            if not isinstance(e, dict):
                continue
            entries.append(
                SkillEntry(
                    url=str(e.get("url") or ""),
                    types=_types_from_names(e.get("types")),
                    page_type=str(e.get("page_type") or ""),
                    data_type=str(e.get("data_type") or ""),
                    site_type=str(e.get("site_type") or ""),
                    fields=[str(f) for f in (e.get("fields") or []) if str(f).strip()],
                    caveats=str(e.get("caveats") or ""),
                    notes=str(e.get("notes") or ""),
                )
            )
        skills.append(
            Skill(
                pattern_id=str(p.get("pattern_id") or f"unnamed-{len(skills)}"),
                regex=regex,
                entries=entries,
            )
        )

    return DomainSkillFile(
        domain=str(data.get("domain") or domain),
        last_updated=data.get("last_updated"),
        skills=skills,
    )


def _entry_to_dict(e: SkillEntry) -> dict[str, Any]:
    # `types` is ALWAYS emitted (empty list = "skip / not a data source" is a
    # meaningful value). Other fields are omitted when empty for clean diffs.
    out: dict[str, Any] = {}
    if e.url:
        out["url"] = e.url
    out["types"] = _type_names(e.types)
    for k, v in (
        ("page_type", e.page_type),
        ("data_type", e.data_type),
        ("site_type", e.site_type),
    ):
        if v:
            out[k] = v
    if e.fields:
        out["fields"] = list(e.fields)
    if e.caveats:
        out["caveats"] = e.caveats
    if e.notes:
        out["notes"] = e.notes
    return out


def serialize_skill_file(dsf: DomainSkillFile) -> str:
    """Serialize a DomainSkillFile back to YAML text."""
    data = {
        "domain": dsf.domain,
        "last_updated": dsf.last_updated or _now_iso(),
        "patterns": [
            {
                "pattern_id": s.pattern_id,
                "regex": s.regex,
                "entries": [_entry_to_dict(e) for e in s.entries],
            }
            for s in dsf.skills
        ],
    }
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )


def render_regex_index(workspace_dir: str | os.PathLike[str] | None = None) -> str:
    """Render the cross-domain regex index for system-prompt injection.

    Emits ``domain:`` headers each followed by that domain's regexes (indented),
    REGEXES ONLY — a compact awareness index; per-entry detail (types/fields/
    caveats) comes from ``lookup_skill``. Returns "" when the library is
    disabled or empty.
    """
    from src.config import settings

    if not settings.skills.enabled:
        return ""
    root = Path(workspace_dir or settings.skills.workspace_dir)
    if not root.exists():
        return ""

    blocks: list[str] = []
    for path in sorted(root.glob(f"*/{CLASSIFY_FILENAME}")):
        try:
            dsf = parse_skill_file(path.read_text(encoding="utf-8"), path.parent.name)
        except OSError:
            continue
        regexes = [s.regex for s in dsf.skills if s.regex]
        if not regexes:
            continue
        blocks.append("\n".join([f"{dsf.domain}:", *(f"  {r}" for r in regexes)]))
    if not blocks:
        return ""

    header = (
        "# Known URL-shape patterns — domain-skill library (PRIORS to verify "
        "against the page, not ground truth)\n"
        "# Each `domain:` lists regexes over url_path classified before. "
        "lookup_skill(etld1, url_path) returns the learned entry (types / fields "
        "/ caveats) for a matching shape.\n\n"
    )
    return header + "\n".join(blocks) + "\n"


# ──────────────────────────────────────────────────────────────────────
# Library service (singleton)
# ──────────────────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _safe_filename(etld1: str) -> str:
    """Sanitize a domain string for filesystem use (no traversal / weird chars)."""
    return _SLUG_RE.sub("-", (etld1 or "").lower()).strip("-") or "unknown"


class SkillLibrary:
    """Per-process singleton over the filesystem skill store.

    * Lazy load: first ``load(etld1)`` parses the file and caches it.
    * ``propose(etld1, skill)`` stages a pattern (+entries); ``flush()``
      merges all pending into disk atomically.
    * Per-domain asyncio.Lock serializes writes.
    """

    def __init__(self, workspace_dir: str | os.PathLike[str]) -> None:
        self._root = Path(workspace_dir)
        self._cache: dict[str, DomainSkillFile] = {}
        self._pending: dict[str, list[Skill]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, etld1: str) -> asyncio.Lock:
        if etld1 not in self._locks:
            self._locks[etld1] = asyncio.Lock()
        return self._locks[etld1]

    def _path_for(self, etld1: str) -> Path:
        return self._root / _safe_filename(etld1) / CLASSIFY_FILENAME

    async def load(self, etld1: str) -> DomainSkillFile:
        """Return all patterns for one eTLD+1, loading from disk on first hit."""
        if etld1 in self._cache:
            return self._cache[etld1]
        path = self._path_for(etld1)
        if not path.exists():
            dsf = DomainSkillFile(domain=etld1)
            self._cache[etld1] = dsf
            return dsf
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except OSError as exc:
            logger.warning("skill_library.read_failed: %s (%s)", path, exc)
            dsf = DomainSkillFile(domain=etld1)
            self._cache[etld1] = dsf
            return dsf
        dsf = parse_skill_file(text, etld1)
        self._cache[etld1] = dsf
        logger.debug("skill_library.loaded: domain=%s patterns=%d", etld1, len(dsf.skills))
        return dsf

    async def lookup(self, etld1: str, url_path: str) -> list[Skill]:
        """Return patterns whose regex matches ``url_path`` (file order — no
        confidence ranking). Callers take ``pattern.focused_entry(url_path)``."""
        dsf = await self.load(etld1)
        return [s for s in dsf.skills if s.matches(url_path)]

    async def lookup_by_url(self, url: str) -> list[Skill]:
        """Like ``lookup`` but from a full URL: finds skill domains that are a
        suffix of the URL host (so a subdomain URL still matches a bare-domain
        skill) and returns the patterns whose regex matches the path. Robust to
        however the agent computed etld1 when it filed the skill."""
        from urllib.parse import urlparse

        p = urlparse(url)
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return []
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        existing = [d.parent.name for d in self._root.glob(f"*/{CLASSIFY_FILENAME}")]
        cands = sorted(
            (d for d in existing if host == d or host.endswith("." + d)),
            key=len,
            reverse=True,
        )
        out: list[Skill] = []
        for d in cands:
            dsf = await self.load(d)
            out.extend(s for s in dsf.skills if s.matches(path))
        return out

    async def propose(self, etld1: str, skill: Skill) -> None:
        """Stage a pattern (with entries) for write at flush time."""
        self._pending.setdefault(etld1, []).append(skill)

    async def flush(self) -> int:
        """Write all pending proposals to disk; return total patterns changed."""
        if not self._pending:
            return 0
        snapshot = self._pending
        self._pending = {}
        total = 0
        for etld1, proposals in snapshot.items():
            async with self._lock_for(etld1):
                dsf = await self._reload(etld1)
                changed = _merge_proposals(dsf, proposals)
                if changed == 0:
                    continue
                await self._write_atomic(etld1, dsf)
                self._cache[etld1] = dsf
                total += changed
        logger.info("skill_library.flushed: domains=%d patterns_changed=%d", len(snapshot), total)
        return total

    # ── self-correction: update / delete / consolidate (no record_use) ──

    async def delete(self, etld1: str, pattern_id: str, reason: str = "") -> bool:
        """Hard-remove a whole pattern. Returns whether anything was removed."""
        async with self._lock_for(etld1):
            dsf = await self._reload(etld1)
            before = len(dsf.skills)
            dsf.skills = [s for s in dsf.skills if s.pattern_id != pattern_id]
            removed = before != len(dsf.skills)
            if removed:
                await self._write_atomic(etld1, dsf)
                self._cache[etld1] = dsf
                logger.info(
                    "skill_library.deleted: etld1=%s pattern_id=%s reason=%r",
                    etld1,
                    pattern_id,
                    reason[:200],
                )
            return removed

    async def update(
        self,
        etld1: str,
        pattern_id: str,
        *,
        regex: str | None = None,
        url: str | None = None,
        types: list[DataSourceType] | None = None,
        entry_patch: dict[str, Any] | None = None,
    ) -> bool:
        """Correct an existing pattern. ``regex`` replaces the pattern's regex;
        ``types``/``entry_patch`` patch the entry whose ``url`` matches (or the
        first entry if ``url`` is None). Returns True iff anything changed.

        This is the in-run "I verified the page — the skill was wrong" path.
        """
        async with self._lock_for(etld1):
            dsf = await self._reload(etld1)
            target = next((s for s in dsf.skills if s.pattern_id == pattern_id), None)
            if target is None:
                return False
            changed = False
            if regex is not None and regex != target.regex:
                try:
                    re.compile(regex)
                except re.error as exc:
                    raise ValueError(f"invalid regex {regex!r}: {exc}") from exc
                target.regex = regex
                changed = True
            if (types is not None or entry_patch) and target.entries:
                ent = (
                    next((e for e in target.entries if url and e.url == url), None)
                    or target.entries[0]
                )
                if types is not None and types != ent.types:
                    ent.types = list(types)
                    changed = True
                for k, v in (entry_patch or {}).items():
                    if hasattr(ent, k) and getattr(ent, k) != v:
                        setattr(ent, k, v)
                        changed = True
            if changed:
                await self._write_atomic(etld1, dsf)
                self._cache[etld1] = dsf
                logger.info("skill_library.updated: etld1=%s pattern_id=%s", etld1, pattern_id)
            return changed

    async def consolidate(self, etld1: str) -> dict[str, Any]:
        """LLM merge-duplicates pass: collapse patterns that index the SAME URL
        shape into one (union their entries; keep/widen one regex). Used to
        generalize the conservative tight regexes proposed from harness truth.

        Returns ``{before, after, merged_groups, llm_failed}``.
        """
        from src.services.llm import llm_service

        out: dict[str, Any] = {"before": 0, "after": 0, "merged_groups": 0, "llm_failed": False}
        async with self._lock_for(etld1):
            dsf = await self._reload(etld1)
            out["before"] = len(dsf.skills)
            if len(dsf.skills) < 2:
                out["after"] = out["before"]
                return out

            payload = [
                {
                    "pattern_id": s.pattern_id,
                    "regex": s.regex,
                    "example_urls": [e.url for e in s.entries if e.url][:5],
                }
                for s in dsf.skills
            ]

            class _MergeGroup(BaseModel):
                keep_pattern_id: str
                merge_into_pattern_ids: list[str]
                merged_regex: str | None = None

            class _Verdict(BaseModel):
                groups: list[_MergeGroup] = Field(default_factory=list)

            prompt = (
                f"Domain: {etld1}\n"
                f"Patterns (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
                "Identify groups of patterns whose REGEXES index the SAME URL "
                "shape and should be ONE pattern. For each group pick the cleanest "
                "`keep_pattern_id`, list `merge_into_pattern_ids` (excluding the "
                "keeper), and optionally a tighter/wider `merged_regex` that covers "
                "all of them. Patterns with genuinely different URL shapes are NOT "
                "duplicates — leave them. Empty groups if nothing merges."
            )
            try:
                verdict, _ = await llm_service.complete_structured(
                    messages=[{"role": "user", "content": prompt}],
                    response_model=_Verdict,
                    profile="type_classifier",
                    model_tier="fast",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("skill_library.consolidate.llm_failed: etld1=%s %s", etld1, exc)
                out["llm_failed"] = True
                out["after"] = out["before"]
                return out

            by_id = {s.pattern_id: s for s in dsf.skills}
            removed: set[str] = set()
            for grp in verdict.groups:
                keeper = by_id.get(grp.keep_pattern_id)
                if keeper is None or keeper.pattern_id in removed:
                    continue
                mergees = [
                    by_id[m]
                    for m in grp.merge_into_pattern_ids
                    if m in by_id and m not in removed and m != keeper.pattern_id
                ]
                if not mergees:
                    continue
                seen = {e.url for e in keeper.entries if e.url}
                for m in mergees:
                    for e in m.entries:
                        if e.url and e.url in seen:
                            continue
                        keeper.entries.append(e)
                        if e.url:
                            seen.add(e.url)
                    removed.add(m.pattern_id)
                if grp.merged_regex:
                    try:
                        re.compile(grp.merged_regex)
                        keeper.regex = grp.merged_regex
                    except re.error:
                        pass
                out["merged_groups"] += 1

            if removed:
                dsf.skills = [s for s in dsf.skills if s.pattern_id not in removed]
                await self._write_atomic(etld1, dsf)
                self._cache[etld1] = dsf
            out["after"] = len(dsf.skills)
            logger.info(
                "skill_library.consolidated: etld1=%s before=%d after=%d groups=%d",
                etld1,
                out["before"],
                out["after"],
                out["merged_groups"],
            )
            return out

    # ── internals ──

    async def _reload(self, etld1: str) -> DomainSkillFile:
        self._cache.pop(etld1, None)
        return await self.load(etld1)

    async def _write_atomic(self, etld1: str, dsf: DomainSkillFile) -> None:
        path = self._path_for(etld1)
        path.parent.mkdir(parents=True, exist_ok=True)
        dsf.last_updated = _now_iso()
        text = serialize_skill_file(dsf)

        def _do_write() -> None:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".tmp.classify.",
                suffix=".yaml",
                delete=False,
            ) as tf:
                tf.write(text)
                tf.flush()
                os.fsync(tf.fileno())
                tmp_path = Path(tf.name)
            os.replace(tmp_path, path)

        await asyncio.to_thread(_do_write)
        logger.debug("skill_library.wrote: %s (%d patterns)", path, len(dsf.skills))


def _merge_proposals(dsf: DomainSkillFile, proposals: list[Skill]) -> int:
    """Merge proposed patterns into a DomainSkillFile.

    * New pattern (by pattern_id AND regex) → append.
    * Existing pattern → append its new entries (dedup by url; same-url entry
      is replaced, latest wins).
    Returns the number of patterns added-or-touched.
    """
    by_id = {s.pattern_id: s for s in dsf.skills}
    by_regex = {s.regex: s for s in dsf.skills}
    changed = 0
    for new in proposals:
        existing = by_id.get(new.pattern_id) or by_regex.get(new.regex)
        if existing is None:
            dsf.skills.append(new)
            by_id[new.pattern_id] = new
            by_regex[new.regex] = new
            changed += 1
            continue
        seen = {e.url: i for i, e in enumerate(existing.entries) if e.url}
        for e in new.entries:
            if e.url and e.url in seen:
                existing.entries[seen[e.url]] = e  # replace same-url entry
            else:
                if e.url:
                    seen[e.url] = len(existing.entries)
                existing.entries.append(e)
        changed += 1
    return changed


_library: SkillLibrary | None = None


def get_skill_library() -> SkillLibrary:
    """Return the process-wide SkillLibrary singleton."""
    global _library
    if _library is None:
        from src.config import settings

        _library = SkillLibrary(settings.skills.workspace_dir)
        logger.info("skill_library.init: root=%s", _library._root)
    return _library
