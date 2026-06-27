"""ORM models backing the conversational UI.

Three tables:
  - ``sessions``  — one row per discovery task (= one conversation).
  - ``events``    — the ordered, replayable timeline (monotonic ``seq`` per
                    session). Replaying a session's events in ``seq`` order
                    reconstructs the full transcript; the SAME frontend reducer
                    drives both live streaming and review.
  - ``artifacts`` — pointers to workspace files (task_description, per-site
                    output_sample.json, final_report, task_plan, …).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC: DB cols are TIMESTAMP WITHOUT TIME ZONE (Postgres rejects aware)


class Session(Base):
    """One discovery task / conversation."""

    __tablename__ = "sessions"

    query_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Parent discovery session when this row is a harness/scraper sub-session;
    # NULL for a top-level discovery session. Self-FK so a child cascade-deletes
    # with its parent; indexed for the sidebar's parent→children grouping.
    parent_query_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("sessions.query_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Sidebar display name. Placeholder (truncated query) at creation, then
    # refreshed from parse_intent's `title` field via the session_titled event.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Mirrors the agent's task_description.md Goal; updated on each
    # task_description_committed event.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | running | completed | error | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    run_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_sources: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reserved for future multi-tenant; single-user local defaults to anonymous.
    user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="anonymous"
    )


class Event(Base):
    """One entry in a session's ordered, replayable timeline."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("query_id", "seq", name="uq_events_query_seq"),
        Index("ix_events_query_seq", "query_id", "seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.query_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Monotonic per-session ordinal — the replay key and the frontend dedup cursor.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # dict for most events; list for partial_sources. JSON handles both.
    payload: Mapped[dict | list] = mapped_column(JSON, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Artifact(Base):
    """Pointer to a workspace file produced during a run."""

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_query", "query_id"),
        Index("ix_artifacts_query_site", "query_id", "site_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.query_id", ondelete="CASCADE"),
        nullable=False,
    )
    site_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # task_description | final_report | sources_jsonl | portal_trees |
    # harness_output_sample | harness_task_plan | …
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
