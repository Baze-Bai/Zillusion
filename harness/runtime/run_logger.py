"""Per-session JSONL run log for harness SDK sessions.

Writes one append-only JSONL file per explore / validate SDK session under
``workspaces/<site_id>/run-logs/<UTC-ts>-<role>.jsonl`` — UNCONDITIONALLY,
independent of the CLI's ``--json`` flag / stdout capture. Before this, the
full event trace existed only in the ephemeral stdout stream (and was
suppressed at default verbosity), so a run left no objective machine-readable
record behind. This module fixes that.

Captured records (each line is one JSON object with a ``ts`` + ``event``):
  - ``run_start``    — role, site, config snapshot (model/max_turns/…), git state
  - ``event``        — every normalized event the runner yields (system /
                       assistant_text / tool_use+input / tool_result+content /
                       result / error)
  - ``agent_turn``   — per-assistant-turn ``model`` + token ``usage`` (the only
                       place that reveals which model actually served each turn)
  - ``error``        — exception with a full ``traceback``
  - ``run_complete`` — the final summary dict

Discipline: writes are lock-guarded and never raise into the run (logging must
never break a session). Each session writes its own file.
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def git_state() -> dict[str, Any]:
    """Best-effort git SHA / branch / dirty status. All-None on failure."""
    info: dict[str, Any] = {"sha": None, "branch": None, "dirty": None}
    for key, args in (
        ("sha", ["git", "rev-parse", "HEAD"]),
        ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
    ):
        try:
            info[key] = (
                subprocess.check_output(
                    args, cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL, timeout=2
                )
                .decode()
                .strip()
            )
        except Exception:
            pass
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode()
        info["dirty"] = bool(out.strip())
    except Exception:
        pass
    return info


def jsonable_usage(usage: Any) -> Any:
    """Coerce an SDK usage object/dict into a small JSON-able token-count dict."""
    if usage is None:
        return None
    src = usage if isinstance(usage, dict) else (getattr(usage, "__dict__", None) or {})
    if isinstance(src, dict):
        keys = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        out = {k: src[k] for k in keys if src.get(k) is not None}
        if out:
            return out
    return usage if isinstance(usage, dict) else str(usage)


class RunLogger:
    """Append-only JSONL trace for one SDK session. Never raises into the run."""

    def __init__(
        self,
        *,
        role: str,
        site_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.role = role
        self.site_id = site_id
        self._lock = threading.Lock()
        self.event_count = 0
        slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        base = (
            (PROJECT_ROOT / "workspaces" / site_id / "run-logs")
            if site_id
            else (PROJECT_ROOT / "run-logs")
        )
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = PROJECT_ROOT
        self.path = base / f"{slug}-{role}.jsonl"
        self.write(
            {
                "event": "run_start",
                "role": role,
                "site_id": site_id,
                "config": config or {},
                "git": git_state(),
            }
        )

    def write(self, record: dict[str, Any]) -> None:
        rec = {"ts": _utcnow_iso(), **record}
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
        except Exception:
            return
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            self.event_count += 1
        except Exception:
            pass

    def event(self, ev: dict[str, Any]) -> None:
        """Persist one runner event dict (already carries a ``kind``)."""
        self.write({"event": "event", **ev})


__all__ = ["RunLogger", "git_state", "jsonable_usage"]
