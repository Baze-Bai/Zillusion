"""Unit tests for harness_orchestrator._emit_terminal_dataset — the inline/
agentic terminal-route dataset registration (run_started → run_output_ready
[→ run_field_doc_ready] → run_done), and the disk fallback for the agentic
run id."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from src.services import harness_orchestrator as ho

if TYPE_CHECKING:
    from pathlib import Path


class _FakeHandle:
    def __init__(self, query_id: str = "site-1") -> None:
        self.query_id = query_id
        self.events: list[tuple[str, dict]] = []

    async def publish(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
def ws(tmp_path: Path, monkeypatch) -> Path:
    """A fake site workspace; site_workspace() is patched to return it."""
    d = tmp_path / "workspaces" / "site-1"
    d.mkdir(parents=True)
    monkeypatch.setattr(ho, "site_workspace", lambda sid: d)
    return d


@pytest.fixture
def upsert(monkeypatch) -> AsyncMock:
    m = AsyncMock(return_value=42)
    monkeypatch.setattr(ho, "upsert_artifact", m)
    return m


def _route(ws: Path, route: str, **extra) -> None:
    (ws / "crawl_route.json").write_text(
        json.dumps({"route": route, "rationale": "t", **extra}), encoding="utf-8"
    )


def _run(ws: Path, run_id: str, *, outcome: str = "complete", records: int = 3) -> Path:
    d = ws / "runs" / run_id
    d.mkdir(parents=True)
    (d / "output.json").write_text('[{"a": 1}]', encoding="utf-8")
    (d / "manifest.yaml").write_text(
        f"run_id: {run_id}\nrecord_count: {records}\noutcome: {outcome}\ndimensions:\n",
        encoding="utf-8",
    )
    return d


async def test_deterministic_route_is_untouched(ws: Path, upsert: AsyncMock):
    _route(ws, "deterministic")
    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", None)
    assert h.events == [] and upsert.await_count == 0


async def test_no_route_file_is_untouched(ws: Path, upsert: AsyncMock):
    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", None)
    assert h.events == [] and upsert.await_count == 0


async def test_inline_without_output_is_untouched(ws: Path, upsert: AsyncMock):
    _route(ws, "inline", run_id="inline-x")
    (ws / "runs" / "inline-x").mkdir(parents=True)  # dir but no output.json
    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", None)
    assert h.events == [] and upsert.await_count == 0


async def test_inline_full_sequence_single_file(ws: Path, upsert: AsyncMock):
    _route(ws, "inline", run_id="inline-ab12")
    run_dir = _run(ws, "inline-ab12", outcome="complete", records=7)
    (ws / "task_plan.md").write_text("# fields", encoding="utf-8")

    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", None)

    kinds = [k for k, _ in h.events]
    assert kinds == ["run_started", "run_output_ready", "run_field_doc_ready", "run_done"]

    started = dict(h.events)["run_started"]
    assert started["run_id"] == "inline-ab12" and started["crawl_mode"] == "inline"

    out = dict(h.events)["run_output_ready"]
    # manifest.yaml + task_plan.md are explainers — they don't make this
    # multi-file, so the chip stays a direct output.json link.
    assert out["filename"] == "output.json" and out["content_type"] == "json"
    assert out["record_count"] == 7

    done = dict(h.events)["run_done"]
    assert done["outcome"] == "COMPLETE" and done["record_count"] == 7

    # field-doc snapshot copied into the run dir + registered
    assert (run_dir / "task_plan.md").read_text(encoding="utf-8") == "# fields"
    registered_kinds = {c.kwargs["kind"] for c in upsert.await_args_list}
    assert registered_kinds == {"harness_run_output", "harness_run_field_doc"}


async def test_inline_with_media_registers_bundle(ws: Path, upsert: AsyncMock):
    _route(ws, "inline", run_id="inline-zz")
    run_dir = _run(ws, "inline-zz")
    (run_dir / "media").mkdir()
    (run_dir / "media" / "p.pdf").write_bytes(b"%PDF")

    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", None)

    out = dict(h.events)["run_output_ready"]
    assert out["filename"] == "site-1_inline-zz.zip" and out["content_type"] == "zip"
    bundle_call = next(
        c for c in upsert.await_args_list if c.kwargs["kind"] == "harness_run_output"
    )
    assert bundle_call.kwargs["path"] == str(run_dir)
    assert bundle_call.kwargs["meta"]["bundle"] is True


async def test_agentic_uses_event_hint(ws: Path, upsert: AsyncMock):
    _route(ws, "agentic")
    _run(ws, "run-cafe01", outcome="partial", records=100)

    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", "run-cafe01")

    started = dict(h.events)["run_started"]
    assert started["run_id"] == "run-cafe01" and started["crawl_mode"] == "agentic"
    assert dict(h.events)["run_done"]["outcome"] == "PARTIAL"


async def test_agentic_disk_fallback_filters_prefix(ws: Path, upsert: AsyncMock):
    _route(ws, "agentic")
    _run(ws, "inline-old")  # other-route leftover — must be ignored
    target = _run(ws, "run-late9")

    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", None)

    assert dict(h.events)["run_started"]["run_id"] == "run-late9"
    out_call = next(
        c for c in upsert.await_args_list if c.kwargs["kind"] == "harness_run_output"
    )
    assert out_call.kwargs["path"] == str(target / "output.json")


async def test_outcome_falls_back_to_partial(ws: Path, upsert: AsyncMock):
    _route(ws, "inline", run_id="inline-nm")
    d = ws / "runs" / "inline-nm"
    d.mkdir(parents=True)
    (d / "output.json").write_text("[{}]", encoding="utf-8")  # data but NO manifest

    h = _FakeHandle()
    await ho._emit_terminal_dataset(h, "site-1", None)
    done = dict(h.events)["run_done"]
    assert done["outcome"] == "PARTIAL" and done["reason"]
