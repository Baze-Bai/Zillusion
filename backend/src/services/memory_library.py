r"""Filesystem-backed Memory Library — cross-run prose notes.

The middle learning layer the Discovery agent lacked. The structured
domain-skill library holds URL-shape regex → typed entries; this holds the
NARRATIVE / STRATEGY / WHY that doesn't fit a regex: publisher access caveats,
which discovery strategies pay off for a query class, anti-patterns and
DO-NOT lessons.

Boundary (mirrors the harness memory↔skill split):

  * URL shape → type / fields            → domain-skill library (structured)
  * narrative / strategy / why / caveat  → memory library (this, prose)

Storage (one markdown file per topic):

    memory/<topic-slug>.md

    # <topic-slug>

    ## [2026-06-05T...]
    <body>

    ## [2026-06-06T...]
    <body>

``append`` adds a timestamped section — notes accumulate, never rewritten.
``render_memory_notes`` emits ALL notes' full text for system-prompt injection
at session start (the read path IS injection; no per-note lookup needed).

Concurrency: per-topic asyncio.Lock serializes writes; atomic temp-file
rename so a partial write never corrupts a file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_MD_SUFFIX = ".md"
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_topic(name: str) -> str:
    """Sanitize a topic into a filesystem-safe slug (no traversal / weird chars)."""
    base = (name or "").strip().lower()
    if base.endswith(_MD_SUFFIX):
        base = base[: -len(_MD_SUFFIX)]
    return _SLUG_RE.sub("-", base).strip("-") or "untitled"


def render_memory_notes(workspace_dir: str | os.PathLike[str] | None = None) -> str:
    """Render ALL memory notes (full text) for system-prompt injection.

    The user-chosen read policy is full-text injection at session start: every
    ``<topic>.md`` is concatenated (separated by a rule) under a short header.
    Returns "" when the library is disabled or empty.
    """
    from src.config import settings

    if not settings.memory.enabled:
        return ""
    root = Path(workspace_dir or settings.memory.workspace_dir)
    if not root.exists():
        return ""

    blocks: list[str] = []
    for path in sorted(root.glob(f"*{_MD_SUFFIX}")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            blocks.append(text)
    if not blocks:
        return ""

    header = (
        "# Cross-run memory — prose lessons (narrative / strategy / why; the "
        "structured URL-shape priors are in the skill index above)\n"
        "# Accumulated observations from prior runs. Treat as priors to apply "
        "with judgment, not as commands. Add to them with memory_append.\n\n"
    )
    return header + "\n\n---\n\n".join(blocks) + "\n"


class MemoryLibrary:
    """Per-process singleton over the filesystem memory store.

    * ``index()`` lists topic slugs; ``read(topic)`` returns one note's text.
    * ``append(topic, body)`` adds a timestamped section (create-or-grow).
    * Per-topic asyncio.Lock serializes writes; atomic temp-file rename.
    """

    def __init__(self, workspace_dir: str | os.PathLike[str]) -> None:
        self._root = Path(workspace_dir)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, slug: str) -> asyncio.Lock:
        if slug not in self._locks:
            self._locks[slug] = asyncio.Lock()
        return self._locks[slug]

    def _path_for(self, topic: str) -> Path:
        return self._root / (_safe_topic(topic) + _MD_SUFFIX)

    async def index(self) -> list[str]:
        """List existing topic slugs (filenames without .md)."""
        if not self._root.exists():
            return []
        return sorted(p.stem for p in self._root.glob(f"*{_MD_SUFFIX}"))

    async def read(self, topic: str) -> str:
        """Return one note's full text, or "" if it does not exist."""
        path = self._path_for(topic)
        if not path.exists():
            return ""
        try:
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        except OSError as exc:
            logger.warning("memory_library.read_failed: %s (%s)", path, exc)
            return ""

    async def append(self, topic: str, body: str) -> str:
        """Append a timestamped section to memory/<topic>.md (create if new).

        Returns the topic slug written, or "" if the body was empty.
        """
        body = (body or "").strip()
        if not body:
            return ""
        slug = _safe_topic(topic)
        async with self._lock_for(slug):
            path = self._path_for(slug)
            existing = ""
            if path.exists():
                try:
                    existing = await asyncio.to_thread(path.read_text, encoding="utf-8")
                except OSError:
                    existing = ""
            if not existing.strip():
                existing = f"# {slug}\n"
            section = f"\n## [{_now_iso()}]\n{body}\n"
            await self._write_atomic(slug, existing.rstrip() + "\n" + section)
        logger.info("memory_library.appended: topic=%s bytes=%d", slug, len(body))
        return slug

    async def _write_atomic(self, slug: str, text: str) -> None:
        path = self._path_for(slug)
        path.parent.mkdir(parents=True, exist_ok=True)

        def _do_write() -> None:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".tmp.mem.",
                suffix=".md",
                delete=False,
            ) as tf:
                tf.write(text)
                tf.flush()
                os.fsync(tf.fileno())
                tmp_path = Path(tf.name)
            os.replace(tmp_path, path)

        await asyncio.to_thread(_do_write)
        logger.debug("memory_library.wrote: %s", path)


_library: MemoryLibrary | None = None


def get_memory_library() -> MemoryLibrary:
    """Return the process-wide MemoryLibrary singleton."""
    global _library
    if _library is None:
        from src.config import settings

        _library = MemoryLibrary(settings.memory.workspace_dir)
        logger.info("memory_library.init: root=%s", _library._root)
    return _library
