"""WebSocket route for the human-takeover embedded browser.

When the harness hits a login / captcha / challenge wall during an
orchestrator-driven run, its `browser_request_user_login` (takeover mode) opens
a headed login browser with a CDP debug port and writes
`workspaces/<site_id>/_takeover_request.json`. The frontend connects here; this
route bridges that Chromium's live screencast + the user's input to the browser
canvas via `CDPBridge`. When the user clicks "done" we flip the handshake file
to `status=done`, which unblocks the harness tool (it then `save_auth`s and
continues the crawl authenticated).

Security: the pending handshake file IS the gate — it exists only during an
active takeover and is deleted right after. (Per-user session ownership is a
follow-up for the multi-user deployment.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket

from src.config import settings
from src.services.cdp_bridge import CDPBridge, discover_page_ws
from src.services.harness_orchestrator import site_workspace

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)
router = APIRouter()


def _read_request(req_path: Path) -> dict | None:
    try:
        return json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _mark_done(req_path: Path) -> None:
    data = _read_request(req_path)
    if data is None:
        return
    data["status"] = "done"
    with contextlib.suppress(OSError):
        tmp = req_path.with_suffix(req_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(req_path)


async def _send_safe(ws: WebSocket, payload: dict) -> bool:
    """send_json tolerating an already-disconnected peer. The frontend's dev
    double-mount (and any navigation away) closes the socket before we reply —
    a dead peer is a normal outcome here, not an error worth a traceback."""
    try:
        await ws.send_json(payload)
        return True
    except Exception:  # noqa: BLE001
        return False


@router.websocket("/api/v1/harness/{site_id}/takeover")
async def harness_takeover(ws: WebSocket, site_id: str) -> None:
    # CSWSH guard: the CORS middleware does NOT cover WebSockets, so without this
    # a malicious web page could open this socket from the user's browser and
    # watch/inject into a real takeover. Browsers always send Origin; require it
    # to be allowed. (A missing Origin = non-browser client; still gated by the
    # optional X-API-Key below.)
    origin = ws.headers.get("origin")
    if origin is not None and origin not in settings.app.cors_origins:
        await ws.close(code=1008)
        return
    # Optional shared-secret gate (when APP_API_KEY is set). A WebSocket carries
    # it as a query param:  wss://.../takeover?api_key=...   Empty = open.
    api_key = settings.app.api_key
    if api_key and ws.query_params.get("api_key") != api_key:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        req_path = site_workspace(site_id) / "_takeover_request.json"
    except ValueError:
        await _send_safe(ws, {"type": "error", "code": "bad_site_id", "error": "invalid site_id"})
        with contextlib.suppress(Exception):
            await ws.close()
        return
    data = _read_request(req_path)
    if not data or data.get("status") != "pending":
        # `code` lets the frontend tell this terminal condition (stale canvas on
        # replay of a run that died mid-takeover) from transient bridge errors.
        await _send_safe(ws, {
            "type": "error",
            "code": "no_active_takeover",
            "error": "no active takeover for this site",
        })
        with contextlib.suppress(Exception):
            await ws.close()
        return

    cdp_ws = data.get("cdp_ws") or await discover_page_ws(
        data.get("cdp_http", ""), data.get("page_url")
    )
    if not cdp_ws:
        await _send_safe(ws, {"type": "error", "code": "cdp_unavailable", "error": "CDP endpoint unavailable"})
        with contextlib.suppress(Exception):
            await ws.close()
        return

    vp = data.get("viewport") or {"width": 1280, "height": 900}
    bridge = CDPBridge(cdp_ws)
    try:
        await bridge.connect()
        await bridge.start_screencast(
            max_w=int(vp.get("width", 1280)), max_h=int(vp.get("height", 900)), quality=60
        )
    except Exception as e:  # noqa: BLE001 — surface to the client, don't 500 the WS
        logger.warning("takeover CDP connect failed for %s: %s", site_id, e)
        await _send_safe(ws, {"type": "error", "code": "cdp_connect_failed", "error": f"CDP connect failed: {e}"})
        await bridge.close()
        with contextlib.suppress(Exception):
            await ws.close()
        return

    if not await _send_safe(ws, {
        "type": "ready", "viewport": vp, "page_url": data.get("page_url"),
        "reason": data.get("reason"), "message": data.get("message"),
        "wall_type": data.get("wall_type"),
    }):
        # Peer vanished before the handshake completed — leave the takeover
        # pending so a reconnect can resume it.
        await bridge.close()
        with contextlib.suppress(Exception):
            await ws.close()
        return

    async def pump_frames() -> None:
        async for frame in bridge.frames():
            if frame.get("data"):
                await ws.send_json({"type": "screenshot", "data": frame["data"]})

    async def pump_input() -> None:
        while True:
            msg = await ws.receive_json()
            if msg.get("action") == "done":
                _mark_done(req_path)
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "done"})
                return
            with contextlib.suppress(Exception):
                await bridge.dispatch(msg)

    ft = asyncio.create_task(pump_frames())
    it = asyncio.create_task(pump_input())
    try:
        # Finish when the user clicks done OR the socket drops (either pump ends).
        # A disconnect WITHOUT 'done' leaves the handshake pending so the user can
        # reconnect and resume (the harness keeps waiting up to its timeout).
        await asyncio.wait({ft, it}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (ft, it):
            t.cancel()
        await asyncio.gather(ft, it, return_exceptions=True)
        await bridge.close()
        with contextlib.suppress(Exception):
            await ws.close()


__all__ = ["router"]
