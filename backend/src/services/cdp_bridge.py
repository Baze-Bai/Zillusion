"""Raw Chrome DevTools Protocol bridge for human-takeover streaming.

The backend side of the "embedded browser takeover" feature: it attaches to a
Chromium **page target** over a `--remote-debugging-port` WebSocket (the harness
launches its headed login browser with that port and advertises the target ws in
`_takeover_request.json`), streams JPEG frames via `Page.startScreencast`, and
injects the remote user's mouse/keyboard via the `Input.*` domain.

No playwright dependency — just `websockets` + `httpx` (both already present).
Frame coordinates arrive from the frontend ALREADY in page space (the canvas
scales canvas->page itself), so we dispatch `Input.*` at the given x/y verbatim.

Validated against real Chromium (see D:/Zillusion_work/test_cdp_feasibility.py):
external CDP screencast + input coexists with playwright driving the same browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import websockets

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Special (non-text) keys the frontend sends as {"action":"press","key":...}.
# (key -> (windowsVirtualKeyCode, code)). Printable chars arrive as "type" and go
# through Input.insertText, so this only needs the common control keys.
_SPECIAL_KEYS: dict[str, tuple[int, str]] = {
    "Enter": (13, "Enter"),
    "Tab": (9, "Tab"),
    "Backspace": (8, "Backspace"),
    "Delete": (46, "Delete"),
    "Escape": (27, "Escape"),
    "ArrowUp": (38, "ArrowUp"),
    "ArrowDown": (40, "ArrowDown"),
    "ArrowLeft": (37, "ArrowLeft"),
    "ArrowRight": (39, "ArrowRight"),
    "Home": (36, "Home"),
    "End": (35, "End"),
    "PageUp": (33, "PageUp"),
    "PageDown": (34, "PageDown"),
}

_CMD_TIMEOUT_S = 15.0


async def discover_page_ws(cdp_http: str, want_url: str | None = None) -> str | None:
    """Find a page target's webSocketDebuggerUrl from Chromium's /json/list.
    Used when the handshake file lacks `cdp_ws` (re-discovery fallback)."""
    import httpx

    for _ in range(20):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{cdp_http}/json/list", timeout=2)
                pages = [
                    t for t in r.json()
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                ]
            if pages:
                if want_url:
                    for t in pages:
                        if t.get("url") == want_url:
                            return t["webSocketDebuggerUrl"]
                for t in pages:
                    if t.get("url") not in (None, "", "about:blank"):
                        return t["webSocketDebuggerUrl"]
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001 — endpoint may not be up yet
            pass
        await asyncio.sleep(0.25)
    return None


class CDPBridge:
    """One attached page target: screencast frames out, input events in."""

    def __init__(self, target_ws: str, *, frame_buffer: int = 2) -> None:
        self._url = target_ws
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._frames: asyncio.Queue[dict] = asyncio.Queue(maxsize=max(1, frame_buffer))
        self._reader: asyncio.Task | None = None
        self._closed = False

    async def __aenter__(self) -> CDPBridge:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> CDPBridge:
        # ping_interval=None: Chromium's CDP socket doesn't reliably pong client
        # pings; disabling them avoids spurious closes. wait_for: a dead/stale
        # CDP endpoint (browser already gone) must fail the takeover request
        # fast instead of hanging the WebSocket handler indefinitely.
        self._ws = await asyncio.wait_for(
            websockets.connect(self._url, max_size=None, ping_interval=None),
            timeout=10.0,
        )
        self._reader = asyncio.create_task(self._read_loop())
        await self._cmd("Page.enable")
        await self._cmd("Runtime.enable")
        return self

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif msg.get("method") == "Page.screencastFrame":
                    p = msg.get("params", {})
                    sid = p.get("sessionId")
                    if sid is not None:
                        # Ack immediately so Chromium keeps sending frames.
                        asyncio.create_task(self._safe_ack(sid))
                    # Keep only the freshest frame — drop stale on a slow consumer.
                    if self._frames.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self._frames.get_nowait()
                    self._frames.put_nowait({"data": p.get("data"), "metadata": p.get("metadata")})
        except Exception:  # noqa: BLE001 — socket closed / cancelled
            pass

    async def _safe_ack(self, session_id: int) -> None:
        with contextlib.suppress(Exception):
            await self._cmd("Page.screencastFrameAck", {"sessionId": session_id})

    async def _cmd(self, method: str, params: dict | None = None) -> dict:
        if self._closed or self._ws is None:
            raise RuntimeError("CDP bridge closed")
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        resp = await asyncio.wait_for(fut, _CMD_TIMEOUT_S)
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error']}")
        return resp.get("result", {})

    # ── streaming ───────────────────────────────────────────────────
    async def start_screencast(
        self, *, max_w: int = 1280, max_h: int = 900, quality: int = 60
    ) -> None:
        await self._cmd("Page.startScreencast", {
            "format": "jpeg", "quality": quality,
            "maxWidth": max_w, "maxHeight": max_h, "everyNthFrame": 1,
        })

    async def frames(self) -> AsyncIterator[dict]:
        """Yield the latest screencast frame {data:<b64 jpeg>, metadata}."""
        while not self._closed:
            yield await self._frames.get()

    async def current_url(self) -> str | None:
        try:
            return await self.evaluate("location.href")
        except Exception:  # noqa: BLE001
            return None

    async def evaluate(self, expression: str) -> Any:
        r = await self._cmd("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return r.get("result", {}).get("value")

    # ── input injection ─────────────────────────────────────────────
    async def dispatch(self, msg: dict) -> None:
        """Apply one frontend input message (page-space coords). 'done' is handled
        by the route, not here."""
        a = msg.get("action")
        if a in ("click", "dblclick"):
            x, y = float(msg.get("x", 0)), float(msg.get("y", 0))
            btn = msg.get("button", "left")
            cc = 2 if a == "dblclick" else int(msg.get("clickCount", 1))
            for t in ("mousePressed", "mouseReleased"):
                await self._cmd("Input.dispatchMouseEvent",
                                {"type": t, "x": x, "y": y, "button": btn, "clickCount": cc})
        elif a == "mousemove":
            await self._cmd(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": float(msg.get("x", 0)), "y": float(msg.get("y", 0))},
            )
        elif a == "scroll":
            await self._cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": float(msg.get("x", 0)), "y": float(msg.get("y", 0)),
                "deltaX": float(msg.get("deltaX", 0)), "deltaY": float(msg.get("deltaY", 0)),
            })
        elif a == "type":
            await self._cmd("Input.insertText", {"text": str(msg.get("text", ""))})
        elif a == "press":
            await self._press_key(str(msg.get("key", "")))

    async def _press_key(self, key: str) -> None:
        if not key:
            return
        base: dict[str, Any] = {"key": key}
        sk = _SPECIAL_KEYS.get(key)
        if sk:
            vk, code = sk
            base.update({"code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
        for t in ("keyDown", "keyUp"):
            await self._cmd("Input.dispatchKeyEvent", {"type": t, **base})

    # ── lifecycle ───────────────────────────────────────────────────
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._cmd("Page.stopScreencast"), 3)
        if self._reader:
            self._reader.cancel()
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["CDPBridge", "discover_page_ws"]
