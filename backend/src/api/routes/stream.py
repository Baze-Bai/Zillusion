"""Live + replay SSE streaming and session-list endpoints.

One endpoint serves both "watch live" and "review a past run":
``GET /stream/{query_id}`` replays the persisted event timeline (optionally
from ``?after_seq=`` / ``Last-Event-ID``) then, if the run is still live, tails
new events. The frontend reduces this ordered stream into a transcript; the
per-event ``seq`` lets it dedup the replay↔live overlap and resume on reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from src.api.sse import sse_event
from src.services import event_store, run_bundle
from src.services.run_registry import run_registry

logger = logging.getLogger(__name__)


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        logger.debug("scratch bundle unlink failed: %s", p)

router = APIRouter(prefix="/api/v1", tags=["stream"])

# Roots an artifact file is allowed to live under (defense against a bad/forged
# path escaping into the filesystem). stream.py → routes→api→src→backend.
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACT_ROOTS = [
    (_BACKEND_ROOT / "agent-workspace").resolve(),
    (_BACKEND_ROOT.parent / "harness" / "workspaces").resolve(),
]

_CONTENT_TYPE_BY_SUFFIX = {
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".csv": "csv",
    ".tsv": "csv",
    ".yaml": "text",
    ".yml": "text",
    ".txt": "text",
    ".py": "text",
}


def _resolve_artifact_path(raw_path: str) -> Path | None:
    """Resolve + validate an artifact path is under an allowed root."""
    try:
        p = Path(raw_path).resolve()
    except Exception:
        return None
    for root in _ARTIFACT_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    return None

# How long to block on the live queue before re-checking the terminal condition.
# The end-of-stream sentinel handles the common case instantly; this bounds the
# rare "subscribed just after the run finished" race (sentinel already sent to
# the then-current subscribers, not to us).
_LIVE_POLL_TIMEOUT = 10.0

# Replay page size: bounds per-client memory during the DB replay phase while
# keeping the number of round-trips low for typical sessions.
_REPLAY_BATCH = 500


async def event_stream(query_id: str, after_seq: int = 0) -> AsyncIterator[dict]:
    """Replay persisted events (seq > after_seq) then tail live ones.

    Subscribes BEFORE replaying so an event published during replay is queued
    and delivered afterward (deduped by seq), closing the gap between the DB
    snapshot and the live tail.
    """
    handle = run_registry.get(query_id)
    live = handle is not None and not handle.is_terminal
    q = handle.subscribe() if (live and handle is not None) else None
    last_seq = after_seq
    try:
        # Replay in fixed-size batches instead of materializing the whole
        # timeline: a long run can persist tens of thousands of events, and a
        # single load_events() buffered them ALL in memory per reconnecting
        # client before the first byte went out.
        replayed_any = False
        while True:
            batch = await event_store.load_events(
                query_id, after_seq=last_seq, limit=_REPLAY_BATCH
            )
            if not batch and not replayed_any and q is None:
                # No persisted events and no live run — is this even a real session?
                if await event_store.load_session(query_id) is None:
                    yield sse_event(
                        "error",
                        {"message": f"unknown query_id: {query_id}", "query_id": query_id},
                    )
                    return
            for ev in batch:
                last_seq = max(last_seq, ev["seq"])
                yield sse_event(ev["event_type"], ev["payload"], seq=ev["seq"])
            if batch:
                replayed_any = True
            if len(batch) < _REPLAY_BATCH:
                break

        if q is not None and handle is not None:
            while True:
                if handle.is_terminal and q.empty():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=_LIVE_POLL_TIMEOUT)
                except asyncio.TimeoutError:
                    continue
                if item is None:  # end-of-stream sentinel
                    break
                if item["seq"] <= last_seq:  # already replayed → dedup
                    continue
                last_seq = item["seq"]
                yield sse_event(
                    item["event_type"], item["payload"],
                    seq=item["seq"], data_json=item.get("json"),
                )
    finally:
        if q is not None and handle is not None:
            handle.unsubscribe(q)


@router.get("/stream/{query_id}")
async def stream(query_id: str, request: Request, after_seq: int = Query(0, ge=0)):
    """SSE: replay then live-tail. Resume via ?after_seq= or Last-Event-ID."""
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            after_seq = max(after_seq, int(last_event_id))
        except ValueError:
            pass
    return EventSourceResponse(event_stream(query_id, after_seq=after_seq))


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Sidebar list — newest first."""
    return await event_store.list_sessions(limit=limit, offset=offset)


@router.get("/sessions/{query_id}")
async def get_session(query_id: str):
    """Full session detail + artifact pointers (transcript comes via /stream)."""
    sess = await event_store.load_session(query_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sess


class SessionPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@router.patch("/sessions/{query_id}")
async def rename_session(query_id: str, body: SessionPatch):
    """Rename a session (the sidebar title)."""
    if await event_store.load_session(query_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title must be non-empty")
    await event_store.update_session_meta(query_id, title=title)
    return {"ok": True, "title": title}


@router.delete("/sessions/{query_id}")
async def delete_session_route(query_id: str):
    """Delete a session + its scraper child sessions (and their events/artifacts).
    Cancels any live runs first so they don't write to a deleted session."""
    if await event_store.load_session(query_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    for qid in [query_id, *await event_store.child_session_ids(query_id)]:
        h = run_registry.get(qid)
        if h is not None and not h.is_terminal and h.task is not None:
            h.task.cancel()
    deleted = await event_store.delete_session(query_id)
    return {"deleted": deleted}


@router.get("/sessions/{query_id}/artifacts/{artifact_id}")
async def get_artifact_content(query_id: str, artifact_id: int):
    """Return an artifact file's content for the right-hand viewer."""
    art = await event_store.get_artifact(artifact_id)
    if art is None or art["query_id"] != query_id:
        raise HTTPException(status_code=404, detail="artifact not found")

    path = _resolve_artifact_path(art["path"])
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file missing")

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"read failed: {e}") from e

    content_type = _CONTENT_TYPE_BY_SUFFIX.get(path.suffix.lower(), "text")
    return {
        "id": art["id"],
        "kind": art["kind"],
        "site_id": art["site_id"],
        "filename": path.name,
        "content_type": content_type,
        "content": content,
        "meta": art["meta"],
    }


@router.get("/sessions/{query_id}/artifacts/{artifact_id}/download")
async def download_artifact(query_id: str, artifact_id: int):
    """Stream an artifact as an attachment download. Unlike the viewer endpoint
    above (which loads the whole file into a JSON string), this serves the raw
    file — suitable for a full crawl's multi-MB output.json.

    A DIRECTORY artifact (multi-file run deliverable: download-type runs,
    media//file_ref companions) is zipped on request into a scratch tmp file
    (single top-level folder inside) and deleted after the response."""
    art = await event_store.get_artifact(artifact_id)
    if art is None or art["query_id"] != query_id:
        raise HTTPException(status_code=404, detail="artifact not found")

    path = _resolve_artifact_path(art["path"])
    if path is None:
        raise HTTPException(status_code=404, detail="artifact file missing")

    if path.is_dir():
        meta = art.get("meta") or {}
        arc_root = f"{art.get('site_id') or 'site'}_{meta.get('run_id') or 'run'}"
        try:
            # Zipping is blocking disk work — keep it off the event loop.
            tmp = await asyncio.to_thread(run_bundle.create_bundle, path, arc_root)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="bundle has no files") from None
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"bundle failed: {e}") from e
        return FileResponse(
            tmp,
            media_type="application/zip",
            filename=f"{arc_root}.zip",
            background=BackgroundTask(_unlink_quiet, tmp),
        )

    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file missing")

    media = "application/json" if path.suffix.lower() == ".json" else "application/octet-stream"
    # filename= sets Content-Disposition: attachment — forces a download even
    # when the frontend links cross-origin (the <a download> attr alone is
    # ignored cross-origin).
    return FileResponse(path, media_type=media, filename=path.name)
