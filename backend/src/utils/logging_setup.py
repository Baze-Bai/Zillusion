"""Centralised logging configuration.

Two formatters:
  - "plain"  : human-readable, used when stdout is a TTY (dev mode)
  - "json"   : structured one-record-per-line, used in production /
               container deployments (so log shippers can index fields)

Toggle via env var `APP_LOG_FORMAT=json|plain` (defaults to plain).

Every record gets `request_id` and `query_id` attributes injected by
`RequestContextFilter`, so both formatters can reference them.

Why dictConfig over basicConfig:
  - basicConfig only configures a handler if root has none, which means
    re-calling it (e.g., in tests or after lifespan reload) is a silent
    no-op. dictConfig is idempotent and lets us declare filter+formatter
    bindings explicitly.
"""

from __future__ import annotations

import json
import logging
import logging.config
import os
import sys
import warnings
from typing import Any

from src.utils.request_context import RequestContextFilter

# langgraph emits a PendingDeprecationWarning at import time about
# `allowed_objects` defaulting changing in a future version. We don't have
# a stable migration target yet, and the warning fires once per fresh
# Python process — pure noise in container restart logs. Filter it here,
# at module top, so the suppression is in place before any subsequent
# import (in main.py) pulls langgraph in transitively.
warnings.filterwarnings(
    "ignore",
    message=r".*allowed_objects.*",
    category=Warning,
)


# Reserved attributes on a LogRecord that we do NOT want to leak into
# the JSON "extras" payload. (Standard library attrs + injected ones.)
_RESERVED_ATTRS: frozenset[str] = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime",
    "request_id", "query_id",  # we surface these as top-level fields
    "taskName",  # added in py3.12+
})


class JsonFormatter(logging.Formatter):
    """Structured JSON formatter — one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        # Standard fields
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "query_id": getattr(record, "query_id", "-"),
        }

        # Anything passed via `logger.info("...", extra={...})` shows up as
        # extra attributes on the record. Promote them to top-level fields.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)  # cheap serialisability probe
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False)


def _build_config(level: str, fmt: str) -> dict[str, Any]:
    """Build a logging.dictConfig dict.

    fmt: "plain" or "json"
    """
    plain_format = (
        "%(asctime)s [%(levelname)s] [req=%(request_id)s qid=%(query_id)s] "
        "%(name)s: %(message)s"
    )

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_context": {
                "()": "src.utils.request_context.RequestContextFilter",
            },
        },
        "formatters": {
            "plain": {
                "format": plain_format,
            },
            "json": {
                "()": "src.utils.logging_setup.JsonFormatter",
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": fmt,
                "filters": ["request_context"],
            },
        },
        "root": {
            "level": level,
            "handlers": ["stdout"],
        },
        # Quiet some chatty third-party loggers at INFO; bump back via
        # APP_LOG_LEVEL=DEBUG if needed.
        # NB: LiteLLM emits each `completion()` call twice — once via its own
        # named logger (which we capture as JSON) and once via a stdout-bound
        # handler attached to the same logger inside the library (formatted
        # as `HH:MM:SS - LiteLLM:INFO: utils.py:NNNN - ...`). Setting level to
        # WARNING silences both at once. Also disable propagation so the JSON
        # capture path is closed even if litellm re-attaches handlers later.
        "loggers": {
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "uvicorn.access": {"level": "WARNING"},
            "LiteLLM": {"level": "WARNING", "propagate": False},
            "LiteLLM Proxy": {"level": "WARNING", "propagate": False},
            "LiteLLM Router": {"level": "WARNING", "propagate": False},
        },
    }


def configure_logging(level: str = "INFO", fmt: str | None = None) -> None:
    """Configure root logger. Idempotent — safe to call multiple times."""
    if fmt is None:
        fmt = os.environ.get("APP_LOG_FORMAT", "").lower() or (
            "plain" if sys.stdout.isatty() else "json"
        )
    if fmt not in {"plain", "json"}:
        fmt = "plain"

    logging.config.dictConfig(_build_config(level=level, fmt=fmt))

    # Belt-and-braces: ensure the filter is on the root logger even if a
    # third-party module mucked with handlers before we ran. dictConfig
    # already does this, but adding twice is a no-op.
    root = logging.getLogger()
    has_filter = any(isinstance(f, RequestContextFilter) for f in root.filters)
    if not has_filter:
        root.addFilter(RequestContextFilter())

    # litellm attaches a stdout StreamHandler to its own "LiteLLM" logger at
    # import time. dictConfig with `disable_existing_loggers: False` doesn't
    # touch those pre-existing handlers, so each completion() would still
    # print twice (once via litellm's handler, once via root). Strip those
    # handlers explicitly. The level=WARNING from _build_config also helps,
    # but stripping handlers is the only thing that survives litellm
    # re-initialising verbose mode on us.
    for name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = False
        lg.setLevel(logging.WARNING)
