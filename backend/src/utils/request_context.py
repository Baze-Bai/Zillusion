"""Request-scoped context propagation for end-to-end log correlation.

Pattern (ported from typical OpenTelemetry/structured-logging setups):
- An HTTP request enters → middleware mints a `request_id` and binds it to
  a `contextvars.ContextVar`.
- Every log record produced anywhere in the codebase (sync or async,
  including inside LangGraph nodes) is automatically tagged with that
  `request_id` via the `RequestContextFilter` attached to the root logger.
- LangGraph state also carries the same `request_id` so that node-level
  logs *and* graph-event SSE streams share one correlation key.

This module is intentionally dependency-free: just stdlib `contextvars`
and `logging`. Anything else (httpx wrapper, adapters, services) just
calls `get_request_id()` to read the current value.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Any

# ─── ContextVars ─────────────────────────────────────────────────────────
# `default=""` so log records have a stable shape even when no request is
# active (e.g., startup logs, background tasks). Downstream filters render
# empty strings as "-" to keep log lines readable.
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
_query_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "query_id", default=""
)
# Per-request running cost accumulator. Populated by `add_request_cost`
# from inside `LLMService._track`, which adds its `usage.cost_usd` here —
# this lets us capture cost from helper sites that discard the usage tuple
# element (e.g. stage_a/stage_b/authority/type_classifier/finalize) without
# having to thread cost back through every signature.
_request_cost_var: contextvars.ContextVar[float] = contextvars.ContextVar(
    "request_cost", default=0.0
)


def new_request_id() -> str:
    """Mint a fresh request_id (32-char hex, no dashes — easier to grep)."""
    return uuid.uuid4().hex


def short_query_id() -> str:
    """8-char short id used in user-facing SSE events and log prefixes."""
    return uuid.uuid4().hex[:8]


def set_request_id(request_id: str) -> contextvars.Token:
    """Bind a request_id to the current context. Returns a reset token."""
    return _request_id_var.set(request_id)


def get_request_id() -> str:
    """Read the request_id currently bound to this context (or ""). """
    return _request_id_var.get()


def reset_request_id(token: contextvars.Token) -> None:
    _request_id_var.reset(token)


def set_query_id(query_id: str) -> contextvars.Token:
    return _query_id_var.set(query_id)


def get_query_id() -> str:
    return _query_id_var.get()


def reset_query_id(token: contextvars.Token) -> None:
    _query_id_var.reset(token)


def add_request_cost(amount: float) -> None:
    """Add USD cost to the current request's running accumulator.

    Negative or zero amounts are ignored so a missing/zero cost from
    LiteLLM's catalog never decrements the total.
    """
    if not amount or amount <= 0:
        return
    _request_cost_var.set(_request_cost_var.get() + float(amount))


def get_request_cost() -> float:
    """Read the running cost for the current request (0.0 if unbound)."""
    return _request_cost_var.get()


def reset_request_cost() -> contextvars.Token:
    """Zero the request cost and return a token for later restore."""
    return _request_cost_var.set(0.0)


def restore_request_cost(token: contextvars.Token) -> None:
    _request_cost_var.reset(token)


class RequestContextFilter(logging.Filter):
    """Logging filter that injects request_id / query_id onto every record.

    Attached to the root logger in `logging_setup.configure_logging()`.
    Both formatters (plain + JSON) reference these attributes.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        rid = _request_id_var.get()
        qid = _query_id_var.get()
        # Use "-" for empty so log lines stay aligned and grep-friendly.
        record.request_id = rid or "-"
        record.query_id = qid or "-"
        return True


def context_snapshot() -> dict[str, Any]:
    """Return current context as a dict — useful for structured payloads."""
    return {
        "request_id": _request_id_var.get(),
        "query_id": _query_id_var.get(),
    }
