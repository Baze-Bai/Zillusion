"""In-memory live layer over the persisted event timeline.

A discovery (and, in Phase 3, harness) run executes as a detached ``asyncio``
task held in a process-global ``RunRegistry``, keyed by ``query_id``. Every
event the run produces flows through ``RunHandle.publish``: persisted to the
``events`` table FIRST, then fanned out to all live SSE subscribers. This
decouples the run from any single HTTP connection — closing the tab does not
stop the run, and several viewers can watch the same run at once.

Design invariants (see plan RISKS):
  - **persist-then-fanout**: a crash between persist and fanout is safe (replay
    covers it); the reverse would risk a live-only event never landing in DB.
  - **bounded, non-blocking subscribers**: a slow client fills its queue and
    drops events rather than stalling the producer; it re-syncs from the DB via
    ``?after_seq=``. The producer NEVER awaits ``queue.put``.
  - **seq under lock**: the monotonic ``seq`` increment + DB append happen under
    a per-handle lock so concurrent publishers (e.g. the discovery loop and a
    custom-event relay, or multiple harness sites) can't interleave seqs.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from src.services import event_store

logger = logging.getLogger(__name__)

# The live run's steering input queue, made available to the discovery graph
# node via a ContextVar (set by run_executor before astream_events). Same
# mechanism as the run_logger/query_id ContextVars, which are already visible
# inside the node + its tools. None outside a steerable run (e.g. tests).
_current_steer_queue: contextvars.ContextVar["asyncio.Queue | None"] = (
    contextvars.ContextVar("current_steer_queue", default=None)
)


def set_steer_queue(queue: "asyncio.Queue | None") -> contextvars.Token:
    return _current_steer_queue.set(queue)


def get_steer_queue() -> "asyncio.Queue | None":
    return _current_steer_queue.get()


def reset_steer_queue(token: contextvars.Token) -> None:
    _current_steer_queue.reset(token)

# Max events buffered per subscriber before we start dropping (it re-syncs from
# DB). Generous for single-user; bounds memory if a client stalls.
_SUBSCRIBER_QUEUE_MAX = 2000

# Keep terminal handles around this long so a client reconnecting right after a
# run finishes still finds the handle (cosmetic — the DB replay path is correct
# regardless). Pruned lazily on the next start().
_GRACE_SECONDS = 60.0

_TERMINAL = ("completed", "error", "cancelled", "interrupted")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC: DB cols are TIMESTAMP WITHOUT TIME ZONE (Postgres rejects aware)


class RunHandle:
    """Live state for one run. ``status`` is the in-memory source of truth;
    terminal transitions also write the ``sessions`` row via ``mark_done``."""

    def __init__(self, query_id: str, *, start_seq: int = 0) -> None:
        self.query_id = query_id
        self.status: str = "pending"
        self.task: asyncio.Task | None = None
        # Steering channel (Phase 2): the discovery agent's streaming-input
        # prompt drains this; the /steer endpoint pushes user messages here.
        self.input_queue: asyncio.Queue = asyncio.Queue()
        self.started_at: datetime = _utcnow()
        self.finished_at: datetime | None = None
        self.error: str | None = None

        self._subscribers: set[asyncio.Queue] = set()
        # Seeded from the prior phase's max seq so a follow-on phase (harness)
        # continues the same per-session timeline instead of restarting at 1.
        self._seq: int = start_seq
        self._lock = asyncio.Lock()
        self._terminal = False

    # ── publish / subscribe ────────────────────────────────────────────
    async def publish(self, event_type: str, payload: Any) -> int:
        """Assign the next seq, persist, then fan out. Returns the seq."""
        async with self._lock:
            self._seq += 1
            seq = self._seq
            # Persist FIRST (inside the lock so seq order == DB order). A
            # transient DB failure (SQLite busy / lock timeout) must not silently
            # drop the event from the replay timeline, so retry briefly; only
            # after the retries are exhausted do we log-and-continue (the live
            # fan-out below still delivers it to currently-open streams).
            for attempt, delay in enumerate((0.0, 0.1, 0.5)):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await event_store.append_event(self.query_id, seq, event_type, payload)
                    break
                except Exception:
                    if attempt == 2:
                        logger.exception(
                            "run_registry.persist_failed",
                            extra={"event": "run_registry.persist_failed", "query_id": self.query_id, "seq": seq},
                        )
        # Fan out OUTSIDE the lock; order is already fixed by seq. The payload
        # is serialized once here and reused by every SSE subscriber
        # (stream.py passes it through) — re-dumping per subscriber multiplied
        # CPU during fan-out bursts.
        item = {
            "seq": seq,
            "event_type": event_type,
            "payload": payload,
            "json": json.dumps(payload, default=str, ensure_ascii=False),
        }
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning(
                    "run_registry.subscriber_lagging",
                    extra={"event": "run_registry.subscriber_lagging", "query_id": self.query_id, "seq": seq},
                )
        return seq

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def is_terminal(self) -> bool:
        return self._terminal or self.status in _TERMINAL

    # ── steering (Phase 2) ──────────────────────────────────────────────
    def push_steer(self, content: Any) -> bool:
        """Push a steering message into the live agent's input queue. Returns
        False if the run isn't accepting input."""
        if self.status != "running":
            return False
        self.input_queue.put_nowait(content)
        return True

    # ── lifecycle ───────────────────────────────────────────────────────
    async def mark_done(
        self,
        status: str,
        *,
        total_cost_usd: float | None = None,
        n_sources: int | None = None,
        error: str | None = None,
    ) -> None:
        """Idempotently record the terminal state (in-memory + sessions row)."""
        if self._terminal:
            return
        self._terminal = True
        self.status = status
        self.finished_at = _utcnow()
        self.error = error
        try:
            await event_store.update_session_status(
                self.query_id,
                status,
                finished_at=self.finished_at,
                error=error,
                total_cost_usd=total_cost_usd,
                n_sources=n_sources,
            )
        except Exception:
            logger.exception(
                "run_registry.status_write_failed",
                extra={"event": "run_registry.status_write_failed", "query_id": self.query_id},
            )

    def close_subscribers(self) -> None:
        """Push the end-of-stream sentinel (None) to every subscriber."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass


RunCoro = Callable[[RunHandle], Awaitable[None]]


class RunRegistry:
    """Process-global registry of live runs keyed by query_id."""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}

    def get(self, query_id: str) -> RunHandle | None:
        return self._runs.get(query_id)

    def _prune_terminal(self) -> None:
        now = _utcnow()
        stale = [
            qid
            for qid, h in self._runs.items()
            if h.is_terminal
            and h.finished_at is not None
            and (now - h.finished_at).total_seconds() > _GRACE_SECONDS
        ]
        for qid in stale:
            self._runs.pop(qid, None)

    async def start(self, query_id: str, run_coro: RunCoro, *, start_seq: int = 0) -> RunHandle:
        """Create a handle and spawn the supervised background task. ``start_seq``
        seeds the seq counter so a follow-on phase continues the timeline."""
        self._prune_terminal()
        handle = RunHandle(query_id, start_seq=start_seq)
        self._runs[query_id] = handle
        handle.task = asyncio.create_task(self._supervise(handle, run_coro))
        return handle

    async def _supervise(self, handle: RunHandle, run_coro: RunCoro) -> None:
        """Run the body, guaranteeing a terminal status + stream close even on
        crash — an unhandled exception here would otherwise be a silent
        'Task exception was never retrieved'."""
        handle.status = "running"
        try:
            await run_coro(handle)
        except asyncio.CancelledError:
            await handle.mark_done("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — last line of defense
            logger.exception(
                "run_registry.run_crashed",
                extra={"event": "run_registry.run_crashed", "query_id": handle.query_id},
            )
            try:
                await handle.publish("error", {"message": str(exc), "query_id": handle.query_id})
            except Exception:
                pass
            await handle.mark_done("error", error=str(exc))
        else:
            # The run body normally calls mark_done with cost/n_sources; ensure
            # a terminal state even if it returned without doing so.
            if not handle.is_terminal:
                await handle.mark_done("completed")
        finally:
            handle.close_subscribers()
            # Eager eviction after the reconnect grace window. Without this,
            # terminal handles (and their subscriber queues) linger until the
            # NEXT start() call happens to prune them — a backend serving many
            # runs accumulates dead handles in the meantime. The task ref is
            # kept on the handle so it isn't garbage-collected mid-sleep.
            handle._evict_task = asyncio.create_task(  # type: ignore[attr-defined]
                self._evict_after_grace(handle.query_id)
            )

    async def _evict_after_grace(self, query_id: str) -> None:
        await asyncio.sleep(_GRACE_SECONDS)
        h = self._runs.get(query_id)
        if h is not None and h.is_terminal:
            self._runs.pop(query_id, None)


# Process-global singleton.
run_registry = RunRegistry()
