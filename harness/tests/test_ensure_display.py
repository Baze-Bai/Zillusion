"""`Browser._ensure_display` / `_x_display_ready` (headed-takeover virtual display)
and `_cap_text` (the >1MB tool-payload transport guard).

The headed login/takeover browser needs a live X server; on a headless Linux
sandbox _ensure_display starts a managed Xvfb and WAITS for its socket (a fixed
sleep raced under load → 'no XServer'). A set DISPLAY alone is not proof a server
is live. Separately, a tool result larger than the SDK's ~1MB stream-message
buffer crashes the whole explore loop, so big payloads are capped.
"""

from __future__ import annotations

import os

from mcp_server.browser import Browser, _TOOL_PAYLOAD_CHAR_CAP, _cap_text


def test_x_display_ready_needs_live_socket(monkeypatch):
    b = Browser()
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/tmp/.X11-unix/X99")
    assert b._x_display_ready() is True
    # DISPLAY set but NO live socket → not ready (the stale-DISPLAY bug)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert b._x_display_ready() is False
    # no DISPLAY at all → not ready
    monkeypatch.delenv("DISPLAY", raising=False)
    assert b._x_display_ready() is False


async def test_ensure_display_noop_when_server_live(monkeypatch):
    # A live X server (or Windows/macOS) must NOT spawn an Xvfb.
    b = Browser()
    monkeypatch.setattr(Browser, "_x_display_ready", lambda self: True)
    await b._ensure_display()
    assert b._xvfb is None


async def test_ensure_display_noop_on_native_windowing():
    if os.name != "nt":
        import pytest

        pytest.skip("native-windowing no-op only asserted on Windows")
    b = Browser()
    await b._ensure_display()
    assert b._xvfb is None


async def test_ensure_display_raises_when_xvfb_missing(monkeypatch):
    # No live socket + Xvfb binary absent → RAISE (don't silently return), so the
    # takeover tool surfaces a clear reason instead of an opaque chromium 'Missing
    # X server' raised later from a code path that already wrote no handshake.
    import subprocess
    import sys as _sys

    import pytest

    b = Browser()
    monkeypatch.setattr(Browser, "_x_display_ready", lambda self: False)
    monkeypatch.setattr(os, "name", "posix")  # force the Linux branch on any host
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", "")  # so the function's env mutation is restored

    def _no_xvfb(*a, **k):
        raise FileNotFoundError("Xvfb")

    monkeypatch.setattr(subprocess, "Popen", _no_xvfb)
    with pytest.raises(RuntimeError, match="Xvfb binary not found"):
        await b._ensure_display()


def test_cap_text_passes_small_and_truncates_large():
    small = "x" * 100
    assert _cap_text(small) == (small, 100)
    big = "y" * (_TOOL_PAYLOAD_CHAR_CAP + 5000)
    capped, total = _cap_text(big)
    assert total == _TOOL_PAYLOAD_CHAR_CAP + 5000
    assert len(capped) == _TOOL_PAYLOAD_CHAR_CAP  # capped well under 1MB
