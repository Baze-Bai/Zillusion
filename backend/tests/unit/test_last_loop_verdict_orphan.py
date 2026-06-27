"""H11: last_loop_verdict must not 409-lock re-runs when a production run
crashed without emitting a terminal event (backend died mid-run).

The walk-back distinguishes a genuinely in-flight run (live registry handle)
from a dead orphan run_started (no live handle) — the orphan is skipped so the
preceding PASS verdict still unlocks a re-run."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services import harness_orchestrator as ho


def _ev(t, **payload):
    return {"event_type": t, "payload": payload}


async def _verdict(events, *, live=None):
    with patch.object(ho, "load_events", AsyncMock(return_value=events)), patch.object(
        ho.run_registry, "get", lambda sid: live
    ):
        return await ho.last_loop_verdict("site-1")


async def test_clean_pass_no_run():
    assert await _verdict([_ev("harness_site_started"), _ev("harness_site_done", final_verdict="PASS")]) == "PASS"


async def test_terminated_run_does_not_invalidate_pass():
    events = [
        _ev("harness_site_started"),
        _ev("harness_site_done", final_verdict="PASS"),
        _ev("run_started"),
        _ev("run_done"),
    ]
    assert await _verdict(events) == "PASS"


async def test_inflight_run_blocks_when_live_handle_present():
    events = [
        _ev("harness_site_started"),
        _ev("harness_site_done", final_verdict="PASS"),
        _ev("run_started"),  # no terminal
    ]
    live = SimpleNamespace(is_terminal=False)
    assert await _verdict(events, live=live) is None


async def test_orphan_run_started_without_live_handle_unlocks_rerun():
    # Backend crashed mid-run: run_started has no terminal AND no live handle.
    # Must NOT block — the preceding PASS still wins.
    events = [
        _ev("harness_site_started"),
        _ev("harness_site_done", final_verdict="PASS"),
        _ev("run_started"),  # orphan — crash left no run_done/run_error
    ]
    assert await _verdict(events, live=None) == "PASS"


async def test_orphan_with_terminal_handle_also_unlocks():
    events = [
        _ev("harness_site_started"),
        _ev("harness_site_done", final_verdict="PASS"),
        _ev("run_started"),
    ]
    live = SimpleNamespace(is_terminal=True)  # a finished handle is not in-flight
    assert await _verdict(events, live=live) == "PASS"
