"""Persistence API for conversational sessions, their event timeline, and
artifacts. Thin async functions over the ORM models — no FastAPI dependency,
so the background run task can call them directly (not via Depends).

Write path (run): ``create_session`` once, then ``append_event`` per streamed
event, plus ``update_session_meta`` / ``update_session_status`` /
``upsert_artifact`` as the run progresses.

Read path (review + sidebar): ``load_events`` replays a transcript,
``list_sessions`` / ``load_session`` back the sidebar and the detail view.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from src.db.engine import AsyncSessionLocal
from src.db.models import Artifact, Event, Session

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC: DB cols are TIMESTAMP WITHOUT TIME ZONE (Postgres rejects aware)


# Sessions left in these states by a crashed process can never resume in-memory.
_NON_TERMINAL = ("pending", "running")


def _cancel_safe(fn):
    """Shield a DB coroutine from *caller* cancellation.

    aiosqlite runs each statement on a worker thread bridged by a greenlet. If
    the awaiting task is cancelled mid-statement — e.g. an SSE client disconnects
    during the ``/stream`` replay read, or a ``/sessions`` request is abandoned on
    navigation — the session's connection cleanup cannot finish and the
    connection is left checked out. The async pool then GC-finalizes that orphan
    from a *different* task, raising "cancel scope entered in a different task
    than it was entered in", which escapes to the event loop and kills the worker
    (exit 255). That was the SSE-disconnect backend crash.

    Shielding lets the open→execute→close cycle complete even when the caller is
    cancelled; the ``CancelledError`` still propagates to the caller afterward, so
    request/stream teardown is unchanged — only the DB connection is no longer
    abandoned mid-flight."""

    @functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        return await asyncio.shield(fn(*args, **kwargs))

    return _wrapper


async def create_session(
    query_id: str,
    *,
    query_text: str,
    title: str,
    request_id: str | None = None,
    run_config: dict | None = None,
    status: str = "running",
    parent_query_id: str | None = None,
) -> None:
    """Insert the session row. ``title`` is the placeholder (truncated query);
    it is refreshed later via ``update_session_meta`` once parse_intent runs.
    ``parent_query_id`` links a harness/scraper sub-session to its discovery
    parent (NULL for top-level discovery sessions)."""
    async with AsyncSessionLocal() as db:
        db.add(
            Session(
                query_id=query_id,
                parent_query_id=parent_query_id,
                request_id=request_id,
                query_text=query_text,
                title=title,
                status=status,
                run_config=run_config,
                created_at=_utcnow(),
            )
        )
        await db.commit()


async def append_event(
    query_id: str,
    seq: int,
    event_type: str,
    payload: Any,
    ts: datetime | None = None,
) -> None:
    """Persist one timeline event. ``seq`` is assigned by the caller under a
    per-run lock, so collisions shouldn't occur; a duplicate is treated as an
    idempotent no-op (defensive against a retry after a partial publish)."""
    async with AsyncSessionLocal() as db:
        db.add(
            Event(
                query_id=query_id,
                seq=seq,
                event_type=event_type,
                payload=payload,
                ts=ts or _utcnow(),
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.debug(
                "event_store.duplicate_seq",
                extra={"event": "event_store.duplicate_seq", "query_id": query_id, "seq": seq},
            )


async def update_session_meta(
    query_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> None:
    """Refresh title (from parse_intent) and/or description (from
    task_description.md). Only non-None fields are written."""
    values: dict[str, Any] = {}
    if title is not None:
        values["title"] = title
    if description is not None:
        values["description"] = description
    if not values:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Session).where(Session.query_id == query_id).values(**values)
        )
        await db.commit()


async def update_session_status(
    query_id: str,
    status: str,
    *,
    finished_at: datetime | None = None,
    error: str | None = None,
    total_cost_usd: float | None = None,
    n_sources: int | None = None,
) -> None:
    """Update run status + optional terminal metadata."""
    values: dict[str, Any] = {"status": status}
    if finished_at is not None:
        values["finished_at"] = finished_at
    if error is not None:
        values["error"] = error
    if total_cost_usd is not None:
        values["total_cost_usd"] = total_cost_usd
    if n_sources is not None:
        values["n_sources"] = n_sources
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Session).where(Session.query_id == query_id).values(**values)
        )
        await db.commit()


async def upsert_artifact(
    query_id: str,
    *,
    kind: str,
    path: str,
    site_id: str | None = None,
    meta: dict | None = None,
) -> int:
    """Insert (or update by (query_id, site_id, kind)) an artifact pointer.
    Returns the artifact row id (usable as a stable handle for the UI)."""
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(
                select(Artifact).where(
                    Artifact.query_id == query_id,
                    Artifact.site_id.is_(site_id) if site_id is None else Artifact.site_id == site_id,
                    Artifact.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.path = path
            existing.meta = meta
            await db.commit()
            return existing.id
        art = Artifact(
            query_id=query_id,
            site_id=site_id,
            kind=kind,
            path=path,
            meta=meta,
            created_at=_utcnow(),
        )
        db.add(art)
        await db.commit()
        await db.refresh(art)
        return art.id


async def max_seq(query_id: str) -> int:
    """Highest event seq for a session (0 if none). Used to seed a follow-on
    phase (e.g. harness) so its events continue the same timeline."""
    async with AsyncSessionLocal() as db:
        val = (
            await db.execute(select(func.max(Event.seq)).where(Event.query_id == query_id))
        ).scalar_one_or_none()
    return int(val or 0)


async def load_events(
    query_id: str, after_seq: int = 0, limit: int | None = None
) -> list[dict[str, Any]]:
    """Replay a session's events in seq order (optionally only those with
    seq > after_seq, for resume). Each dict is {seq, event_type, payload}.

    ``limit`` lets the SSE replay path page through a long timeline in batches
    instead of materializing tens of thousands of rows in one list."""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Event.seq, Event.event_type, Event.payload)
            .where(Event.query_id == query_id, Event.seq > after_seq)
            .order_by(Event.seq.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await db.execute(stmt)).all()
    return [
        {"seq": seq, "event_type": event_type, "payload": payload}
        for (seq, event_type, payload) in rows
    ]


def _session_summary(s: Session) -> dict[str, Any]:
    return {
        "query_id": s.query_id,
        "parent_query_id": s.parent_query_id,
        # For a scraper child: the discovery source id it was built from — lets
        # the discovery page hide the Build button on already-built sources.
        "source_id": (s.run_config or {}).get("source_id"),
        "title": s.title,
        "query_text": s.query_text,
        "status": s.status,
        "created_at": s.created_at,
        "finished_at": s.finished_at,
        "n_sources": s.n_sources,
        "total_cost_usd": s.total_cost_usd,
    }


async def list_sessions(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Sidebar list — newest first."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Session)
                .order_by(Session.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    return [_session_summary(s) for s in rows]


async def child_session_ids(parent_query_id: str) -> list[str]:
    """All scraper child session ids of a discovery session."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Session.query_id).where(Session.parent_query_id == parent_query_id)
            )
        ).scalars().all()
    return list(rows)


async def delete_session(query_id: str) -> int:
    """Delete a session + its scraper child sessions, cascading to their events
    and artifacts. events/artifacts→sessions has ON DELETE CASCADE; the
    parent_query_id self-FK may lack it on a migrated DB, so children are removed
    explicitly first. Returns the count of top-level rows deleted (0/1)."""
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Session).where(Session.parent_query_id == query_id))
        result = await db.execute(delete(Session).where(Session.query_id == query_id))
        await db.commit()
        return result.rowcount or 0


async def load_session(query_id: str) -> dict[str, Any] | None:
    """Full session detail + its artifacts (transcript is fetched via /stream)."""
    async with AsyncSessionLocal() as db:
        s = (
            await db.execute(select(Session).where(Session.query_id == query_id))
        ).scalar_one_or_none()
        if s is None:
            return None
        arts = (
            await db.execute(
                select(Artifact).where(Artifact.query_id == query_id).order_by(Artifact.id.asc())
            )
        ).scalars().all()
    detail = _session_summary(s)
    detail.update(
        {
            "description": s.description,
            "error": s.error,
            "run_config": s.run_config,
            "request_id": s.request_id,
            "artifacts": [
                {
                    "id": a.id,
                    "site_id": a.site_id,
                    "kind": a.kind,
                    "path": a.path,
                    "meta": a.meta,
                    "created_at": a.created_at,
                }
                for a in arts
            ],
        }
    )
    return detail


async def get_artifact(artifact_id: int) -> dict[str, Any] | None:
    """Fetch one artifact row by id (for the artifact viewer)."""
    async with AsyncSessionLocal() as db:
        a = (
            await db.execute(select(Artifact).where(Artifact.id == artifact_id))
        ).scalar_one_or_none()
    if a is None:
        return None
    return {
        "id": a.id,
        "query_id": a.query_id,
        "site_id": a.site_id,
        "kind": a.kind,
        "path": a.path,
        "meta": a.meta,
    }


async def mark_orphaned_running_interrupted() -> int:
    """On startup: any session still 'pending'/'running' is from a process that
    died mid-run (the in-memory RunRegistry is empty now). Mark them
    'interrupted' so the UI shows a terminal state instead of a perpetual
    spinner, and the /stream replay path closes instead of waiting for a live
    handle that will never exist. Returns the number of rows updated."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Session)
            .where(Session.status.in_(_NON_TERMINAL))
            .values(status="interrupted", finished_at=_utcnow())
        )
        await db.commit()
        return result.rowcount or 0


# Make every public DB coroutine above cancel-safe in one place (rather than a
# per-function decorator) so new DB helpers are covered automatically. Each opens
# an aiosqlite session; shielding the whole call prevents a cancelled caller from
# abandoning the connection mid-cleanup. See ``_cancel_safe``.
for _name, _obj in list(globals().items()):
    if not _name.startswith("_") and asyncio.iscoroutinefunction(_obj):
        globals()[_name] = _cancel_safe(_obj)
del _name, _obj
