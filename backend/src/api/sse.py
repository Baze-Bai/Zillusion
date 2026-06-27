"""SSE (Server-Sent Events) helpers for streaming pipeline progress.

For sse-starlette EventSourceResponse: yield a dict with
'event' and 'data' keys. The library handles the protocol formatting.
"""

from __future__ import annotations

import json
from typing import Any


def sse_event(
    event_type: str,
    data: Any,
    *,
    seq: int | None = None,
    data_json: str | None = None,
) -> dict[str, str]:
    """Build an SSE event payload compatible with sse-starlette.

    Returns a dict {'event': ..., 'data': ...} that EventSourceResponse
    will format into the SSE wire protocol automatically.

    When ``seq`` is given it is emitted as the SSE ``id:`` field — the
    monotonic per-session ordinal the frontend uses to dedup replay-vs-live
    overlap and to resume via ``Last-Event-ID`` / ``?after_seq=``. The ``data``
    shape is left untouched so existing event payload contracts are preserved.

    ``data_json`` short-circuits serialization with an already-serialized
    payload (the live fan-out path serializes once per event at publish time
    and reuses the string for every subscriber).
    """
    event: dict[str, str] = {
        "event": event_type,
        "data": data_json if data_json is not None else json.dumps(data, default=str, ensure_ascii=False),
    }
    if seq is not None:
        event["id"] = str(seq)
    return event
