"""Async SQLAlchemy engine + session factory for the persistence layer.

Single-user local app: the default DB is SQLite (``sqlite+aiosqlite``). The
URL comes from ``settings.db.url`` (env ``DB_URL``), so Postgres remains
possible by overriding that env var.

Schema is bootstrapped with ``Base.metadata.create_all`` (``init_db``) rather
than Alembic — there are no migrations yet and the schema is greenfield. If the
schema ever needs versioned evolution (multi-tenant, long-lived prod), wire
Alembic against this same ``Base`` / ``engine``; nothing here precludes it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings
from src.db.base import Base

logger = logging.getLogger(__name__)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _sqlite_path(url: str) -> str:
    """Extract the filesystem path from a sqlite URL (``...:///<path>``)."""
    return url.split(":///", 1)[-1] if ":///" in url else ""


_connect_args: dict = {}
if _is_sqlite(settings.db.url):
    # aiosqlite may be touched across threads as anyio hops worker threads;
    # disable the same-thread guard and let SQLAlchemy pooling + WAL handle it.
    _connect_args = {"check_same_thread": False}

engine: AsyncEngine = create_async_engine(
    settings.db.url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
)


if _is_sqlite(settings.db.url):

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """WAL so the SSE replay path can read while the run appends events;
        foreign_keys ON to enforce events/artifacts → sessions; a busy_timeout
        so a brief writer lock doesn't surface as an immediate 'database is
        locked' error."""
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()


AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


def _migrate_sqlite(conn) -> None:  # noqa: ANN001
    """Lightweight additive migration (no Alembic): add columns introduced after
    a DB file was first created. ``create_all`` never ALTERs an existing table,
    so a column added to a model later is invisible to an old DB. Each ALTER is
    PRAGMA-guarded, so this is idempotent."""
    from sqlalchemy import text

    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))}
    if "parent_query_id" not in cols:
        conn.execute(text("ALTER TABLE sessions ADD COLUMN parent_query_id VARCHAR(64)"))
        logger.info(
            "db.migrate",
            extra={"event": "db.migrate", "change": "sessions.parent_query_id added"},
        )


async def init_db() -> None:
    """Create all tables if absent. Idempotent; safe to call on every startup."""
    if _is_sqlite(settings.db.url):
        db_path = _sqlite_path(settings.db.url)
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # Import models so they register on Base.metadata before create_all.
    from src.db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _is_sqlite(settings.db.url):
            await conn.run_sync(_migrate_sqlite)

    logger.info(
        "db.init_complete",
        extra={"event": "db.init_complete", "url": settings.db.url},
    )


async def dispose_engine() -> None:
    """Dispose the connection pool on shutdown."""
    await engine.dispose()
