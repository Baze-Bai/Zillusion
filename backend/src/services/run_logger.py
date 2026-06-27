"""Per-discovery-run structured event log.

One JSONL file per ``/discover`` request, captured at
``agent-workspace/run-logs/<UTC-timestamp>-<query_id_short>.log``.

Captures:
  - ``run_start`` / ``run_complete`` — boundary events with query text + totals
  - ``node_start`` / ``node_complete`` — LangGraph node transitions
  - ``agent_text`` — assistant-text blocks from the agent SDK (model thinking)
  - ``tool_call`` / ``tool_result`` / ``tool_error`` — MCP tool I/O
  - ``crawl_node`` — per-node visits inside ``crawl_list_tree``
  - ``agent_done`` — agent loop totals (cost / duration / turns)
  - ``error`` — pipeline-level exceptions

Excluded by design (per project requirement — these are SDK-debug noise,
not "what happened in this run"):
  - Claude SDK ``session_id`` / ``cwd``
  - MCP ``tool_use_id``
  - Anthropic model name / fallback_model name / request_id at SDK level
  - Internal LangGraph node/event hashes

Mechanism:
  - One ``RunLogger`` instance per request, held in a ``ContextVar`` so
    deep code paths (tools, sub-coroutines, gathered tasks) reach it
    without explicit plumbing. ContextVars are inherited across
    ``asyncio.create_task`` per PEP 567.
  - The module-level ``log_event(event, data)`` helper is the only public
    write API. It silently no-ops when no RunLogger is active, so tools
    invoked from CLI / tests / outside ``/discover`` don't error out.
  - Writes are synchronous (file open-append-close per event). At ~few-
    hundred events per run this is negligible (< 100ms total). Swap to
    a buffered writer if event volume balloons later.
  - Write failures are swallowed — logging must NEVER break a run.

Co-existence with stdlib ``logging``: this is a structured RUN trace, not
a replacement for ``logger.info``. The existing ``logger.info / .warning``
calls continue to flow to wherever Python's logging is configured. The
RunLogger writes are additive.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_RUN_LOG_DIR = Path("agent-workspace/run-logs")

# Module-level ContextVar. Defaults to None so tools called outside a
# /discover request (CLI, unit tests) silently no-op.
_current_run_logger: contextvars.ContextVar["RunLogger | None"] = (
    contextvars.ContextVar("current_run_logger", default=None)
)


# ──────────────────────────────────────────────────────────────────────
# Session-scoped diagnostics state (Landing point 1)
#
# Per-run mutable state used by tool wrappers to emit signals that need
# cross-call context (e.g. "I have already fetched this URL this run").
# Lives in a ContextVar alongside the RunLogger so it inherits the same
# async-task propagation. Initialized whenever set_run_logger is called
# so every existing caller (discover.py + e2e scripts) gets it for free.
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SessionDiagnostics:
    """Mutable per-run state for signal emission.

    Only tracks state that signals NEED. Aggregation across signals
    (histograms, counts) happens at writeback time by reading the run-log
    JSONL — not from this struct.
    """

    fetched_urls: set[str] = field(default_factory=set)


_current_session_diagnostics: contextvars.ContextVar[
    "SessionDiagnostics | None"
] = contextvars.ContextVar("current_session_diagnostics", default=None)


def get_session_diagnostics() -> "SessionDiagnostics | None":
    """Return the active SessionDiagnostics for this context, or None."""
    return _current_session_diagnostics.get()


class RunLogger:
    """Append-only JSONL run-trace file scoped to one /discover request.

    Use the module-level ``log_event(event, data)`` helper to emit records;
    holding a direct reference to the instance is rarely needed. The
    instance owns its file path + writes; tear-down is implicit (the
    file handle is opened per write).
    """

    def __init__(self, query_id: str, query_text: str = "") -> None:
        self.query_id = query_id
        # Keep the original query text as an attribute so downstream tools
        # (e.g. crawl_list_tree's auto-classify) can derive task context
        # directly from the user's request without explicit hand-off.
        self.query_text = query_text or ""
        # Serialize file appends. log_event can fire concurrently from gathered
        # async tasks AND from SDK/httpx worker threads; the per-event
        # open-append-close is otherwise non-atomic for large lines, producing
        # truncated/interleaved JSONL (~1% of lines observed under 40+ agent
        # turns). A lock around the write makes every line atomic.
        self._lock = threading.Lock()
        slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        # Use first 8 chars of query_id for the filename — short_query_id
        # already returns a short hash so this is fine; if a caller passes
        # a full UUID we'd still keep the file name reasonable.
        short = (query_id or "anon")[:8]
        try:
            _RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            _logger.debug("run_logger could not create %s: %s", _RUN_LOG_DIR, e)
        self.path = _RUN_LOG_DIR / f"{slug}-{short}.log"
        # Header record — written synchronously so the file exists from t=0
        # and external watchers (tail -f, log shippers) can attach.
        self._write({
            "event": "run_start",
            "data": {
                "query_id": query_id,
                # Full query text — no truncation. Per "全部保存" requirement.
                "query": query_text or "",
            },
        })

    def log(self, event: str, data: dict[str, Any]) -> None:
        """Append one event record. Safe to call from any sync/async context."""
        self._write({"event": event, "data": data})

    def _write(self, record: dict[str, Any]) -> None:
        # `ts` first for human skim-ability; JSON object order is preserved
        # in Python ≥ 3.7.
        full = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **record,
        }
        try:
            line = json.dumps(full, ensure_ascii=False, default=str)
        except Exception as e:
            _logger.debug("run_logger encode failed (%s): %s", e, record.get("event"))
            return
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            _logger.debug("run_logger write failed: %s", e)


# ──────────────────────────────────────────────────────────────────────
# ContextVar management
# ──────────────────────────────────────────────────────────────────────


def set_run_logger(rl: "RunLogger | None") -> contextvars.Token:
    """Bind a RunLogger to the current async context. Returns a reset
    token — pass it to ``reset_run_logger`` in your ``finally`` block.

    Also initializes a fresh ``SessionDiagnostics`` in a sibling ContextVar
    so tool wrappers (and the diagnostics_writeback node) see consistent
    per-run state. The returned token is a tuple — but callers should
    treat it as opaque and only pass it back to ``reset_run_logger``.
    """
    rl_token = _current_run_logger.set(rl)
    diag_token = _current_session_diagnostics.set(
        SessionDiagnostics() if rl is not None else None
    )
    return (rl_token, diag_token)  # type: ignore[return-value]


def reset_run_logger(token: Any) -> None:
    """Restore both ContextVars to their prior values. Safe to call with a
    stale token — failures are swallowed.

    Accepts either the tuple returned by ``set_run_logger`` (new shape) or
    a bare contextvars.Token (legacy shape, for any caller that pickled an
    older token before this change rolled out — there shouldn't be any in
    practice, but the cost of accepting both is one isinstance check).
    """
    rl_token = token
    diag_token: contextvars.Token | None = None
    if isinstance(token, tuple) and len(token) == 2:
        rl_token, diag_token = token
    try:
        _current_run_logger.reset(rl_token)
    except Exception:
        pass
    if diag_token is not None:
        try:
            _current_session_diagnostics.reset(diag_token)
        except Exception:
            pass


def get_run_logger() -> "RunLogger | None":
    """Return the active RunLogger for this context, or None."""
    return _current_run_logger.get()


# ──────────────────────────────────────────────────────────────────────
# Public emit API
# ──────────────────────────────────────────────────────────────────────


def log_event(event: str, data: dict[str, Any]) -> None:
    """Emit one structured record to the active RunLogger (if any).

    Silent no-op when no RunLogger is bound — this is intentional so that
    tools invoked outside the /discover flow (CLI scripts, unit tests,
    one-shot smoke tests) don't error or require setup.

    The ``data`` dict should contain only JSON-serializable values; any
    encoding failure is swallowed (logged at DEBUG).
    """
    rl = _current_run_logger.get()
    if rl is None:
        return
    try:
        rl.log(event, data)
    except Exception as e:
        _logger.debug("log_event(%s) failed: %s", event, e)
