"""Run-agent supervision package: kill_crawl + wake-condition alerts.

Deterministic (no LLM): spawns tiny throwaway workflow.py subprocesses through
the REAL run_exec streaming pump and asserts:

  - kill_crawl terminates a running crawl (state=killed, reason 'agent kill: …'),
    is idempotent-safe on a terminal crawl, and is BLOCKED during a takeover;
  - a kill salvages incrementally-flushed partial output (the contract added to
    hypothesis-loop VALIDATE_E2E) — record_count / output.json materialized;
  - poll_with_wait returns EARLY (wake_reason='alert') on a wake_pattern hit,
    a stall warning at STALL_WARN_FRACTION of the stall timeout, and surfaces a
    traceback alert; an invalid pattern is reported, not fatal.

Timing: detectors ride the pump's 5s tick (CHECK_INTERVAL_S), so stall asserts
use generous bounds. Whole file ~30s.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

from runtime import run_exec
from runtime.run_tools import poll_with_wait

# ── tiny throwaway workflows ─────────────────────────────────────────

WF_LOOP = """\
import time
for i in range(1000):
    print(f"beat {i}", flush=True)
    time.sleep(0.3)
"""

WF_FLUSH_THEN_LOOP = """\
import json, os, time
records = [{"id": i} for i in range(3)]
tmp = "output_sample.json.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(records, f)
os.replace(tmp, "output_sample.json")
print("flushed 3", flush=True)
for i in range(1000):
    print(f"beat {i}", flush=True)
    time.sleep(0.3)
"""

WF_CAPTCHA_AFTER_1S = """\
import time
print("start", flush=True)
time.sleep(1.0)
print("[RETRY] 0003803550 attempt 1/2: captcha_redirect - waiting", flush=True)
time.sleep(60)
"""

WF_ONE_LINE_THEN_SILENCE = """\
import time
print("one line then silence", flush=True)
time.sleep(120)
"""

WF_RAISE = """\
print("about to fail", flush=True)
raise RuntimeError("boom")
"""


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_site(tmp_path, monkeypatch):
    """Factory: (workflow_src) -> (site_id, run_id) with run_exec._workspace
    monkeypatched into tmp_path so nothing touches the real workspaces/."""
    ws_root = tmp_path / "workspaces"

    def _ws(site_id: str) -> Path:
        return ws_root / site_id

    monkeypatch.setattr(run_exec, "_workspace", _ws)

    def _make(workflow_src: str) -> tuple[str, str]:
        site_id = f"t-{uuid.uuid4().hex[:8]}"
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        ws = _ws(site_id)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "workflow.py").write_text(workflow_src, encoding="utf-8")
        return site_id, run_id

    return _make


async def _start(site_id: str, run_id: str, **kw) -> dict:
    res = await run_exec.start_crawl(site_id, run_id, **kw)
    assert res.get("started"), res
    return res


async def _wait_output(site_id: str, run_id: str, timeout: float = 15.0) -> dict:
    """Block until the crawl is running AND has emitted at least one line."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        prog = run_exec.get_progress(site_id, run_id)
        if prog.get("state") == "running" and prog.get("lines_emitted", 0) > 0:
            return prog
        await asyncio.sleep(0.2)
    raise AssertionError(f"never running+output: {run_exec.get_progress(site_id, run_id)}")


# ── kill_crawl ───────────────────────────────────────────────────────


async def test_kill_crawl_terminates_running_crawl(fake_site):
    site_id, run_id = fake_site(WF_LOOP)
    try:
        await _start(site_id, run_id)
        await _wait_output(site_id, run_id)

        res = await run_exec.kill_crawl(site_id, run_id, "test: structural doom")
        assert res["killed"] is True
        assert res["state"] == "killed"
        assert res["killed_reason"] == "agent kill: test: structural doom"
        assert res["exit_code"] is not None

        prog = run_exec.get_progress(site_id, run_id)
        assert prog["state"] == "killed"

        again = await run_exec.kill_crawl(site_id, run_id, "double tap")
        assert again["killed"] is False
        assert "terminal" in again["reason"]
    finally:
        await run_exec.cleanup_run(site_id, run_id)


async def test_kill_salvages_flushed_partial_output(fake_site):
    site_id, run_id = fake_site(WF_FLUSH_THEN_LOOP)
    try:
        await _start(site_id, run_id)
        await _wait_output(site_id, run_id)

        res = await run_exec.kill_crawl(site_id, run_id, "test: salvage partials")
        assert res["killed"] is True
        # post_process ran inside kill_crawl's pump-wait: the incremental
        # flush left output_sample.json -> materialized as output.json.
        assert res["record_count"] == 3
        assert res["output_path"] and res["output_path"].endswith("output.json")
        assert Path(res["output_path"]).exists()
    finally:
        await run_exec.cleanup_run(site_id, run_id)


async def test_kill_blocked_during_takeover(fake_site):
    site_id, run_id = fake_site(WF_LOOP)
    try:
        await _start(site_id, run_id)
        await _wait_output(site_id, run_id)
        h = run_exec.get_handle(site_id, run_id)
        assert h is not None

        run_exec.begin_takeover(h)
        blocked = await run_exec.kill_crawl(site_id, run_id, "should be blocked")
        assert blocked["killed"] is False
        assert "takeover" in blocked["reason"]

        run_exec.end_takeover(h)
        res = await run_exec.kill_crawl(site_id, run_id, "after takeover")
        assert res["killed"] is True
    finally:
        await run_exec.cleanup_run(site_id, run_id)


async def test_kill_unknown_run():
    res = await run_exec.kill_crawl("no-such-site", "no-such-run", "x")
    assert res["killed"] is False


# ── wake conditions ──────────────────────────────────────────────────


async def test_poll_wakes_early_on_pattern(fake_site):
    site_id, run_id = fake_site(WF_CAPTCHA_AFTER_1S)
    try:
        await _start(site_id, run_id)
        t0 = time.monotonic()
        prog = await poll_with_wait(site_id, run_id, wait_s=30.0, wake_pattern="(?i)captcha")
        waited = time.monotonic() - t0

        assert prog["wake_reason"] == "alert", prog
        assert waited < 15.0, f"pattern wake took {waited:.1f}s (should be ~1-2s)"
        kinds = [a["kind"] for a in prog.get("new_alerts", [])]
        assert "pattern" in kinds
        hit = next(a for a in prog["new_alerts"] if a["kind"] == "pattern")
        assert "captcha_redirect" in hit["detail"]
        assert prog["state"] == "running"  # woke the poll, killed nothing
    finally:
        await run_exec.cleanup_run(site_id, run_id)


async def test_stall_warning_then_stall_kill(fake_site):
    site_id, run_id = fake_site(WF_ONE_LINE_THEN_SILENCE)
    try:
        # stall kill at 6s -> warning due at 3s; both surface on the pump's
        # ~5s tick. Wall cap high so only the stall path is in play.
        await _start(site_id, run_id, stall_timeout_s=6.0, wall_clock_cap_s=120.0)
        await _wait_output(site_id, run_id)

        prog = await poll_with_wait(site_id, run_id, wait_s=30.0)
        assert prog["wake_reason"] == "alert", prog
        kinds = [a["kind"] for a in prog.get("new_alerts", [])]
        assert "stall_warning" in kinds
        assert prog["state"] == "running"  # warning fired BEFORE the hard kill

        prog2 = await poll_with_wait(site_id, run_id, wait_s=30.0)
        assert prog2["wake_reason"] == "terminal", prog2
        assert prog2["state"] == "killed"
        assert "stall_timeout" in (prog2.get("killed_reason") or "")
    finally:
        await run_exec.cleanup_run(site_id, run_id)


async def test_traceback_raises_alert(fake_site):
    site_id, run_id = fake_site(WF_RAISE)
    try:
        await _start(site_id, run_id)
        # Poll until terminal (the process dies fast; an alert-wake may land
        # first — keep polling through it).
        end = time.monotonic() + 20.0
        prog = await poll_with_wait(site_id, run_id, wait_s=10.0)
        while prog["wake_reason"] != "terminal" and time.monotonic() < end:
            prog = await poll_with_wait(site_id, run_id, wait_s=10.0)

        assert prog["state"] == "done"
        assert prog["exit_code"] not in (0, None)
        kinds = [a["kind"] for a in prog.get("alerts_recent", [])]
        assert "traceback" in kinds
    finally:
        await run_exec.cleanup_run(site_id, run_id)


async def test_invalid_wake_pattern_reported_not_fatal(fake_site):
    site_id, run_id = fake_site(WF_LOOP)
    try:
        await _start(site_id, run_id)
        await _wait_output(site_id, run_id)
        prog = await poll_with_wait(site_id, run_id, wait_s=0.0, wake_pattern="[")
        assert "wake_pattern_error" in prog
        assert prog["state"] == "running"
    finally:
        await run_exec.cleanup_run(site_id, run_id)
