"""FastAPI application factory — entry point for the discovery agent backend."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from src.api.middleware import RequestLoggingMiddleware
from src.api.sse import sse_event
from src.config import settings
from src.utils.logging_setup import configure_logging

logger = logging.getLogger(__name__)

# Single source of truth for the build identifier. Surfaced both via the
# FastAPI app's OpenAPI metadata and the /health endpoint so clients can
# correlate their request behavior against a specific build during diagnostics.
SERVICE_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    # Startup — structured logging with request_id propagation.
    configure_logging(level=settings.app.log_level)
    logger.info("Starting DataSource Discovery Agent...")

    # Persistence — create tables (idempotent) and reconcile any sessions left
    # in a non-terminal state by a previous crash, so the UI doesn't show a
    # perpetual spinner for a run that will never resume.
    from src.db.engine import init_db
    await init_db()
    from src.services.event_store import mark_orphaned_running_interrupted
    n_interrupted = await mark_orphaned_running_interrupted()
    if n_interrupted:
        logger.info(
            "startup.sessions_reconciled",
            extra={"event": "startup.sessions_reconciled", "count": n_interrupted},
        )

    # Setup observability
    from src.services.observability import setup_observability
    setup_observability()

    # Connect cache (cache_service.connect() emits its own structured logs;
    # we only need to swallow the exception here so a missing Redis doesn't
    # block startup — the system degrades gracefully without cache).
    from src.services.cache import cache_service
    try:
        await cache_service.connect()
    except Exception:
        logger.warning(
            "startup.cache_unavailable",
            extra={"event": "startup.cache_unavailable"},
        )

    # Register adapters
    from src.adapters.registry import register_all_adapters
    register_all_adapters()

    # Discover user plugins (ported from Claude Code's skill discovery)
    from src.services.plugin_registry import plugin_registry
    from pathlib import Path
    user_plugins_dir = Path.home() / ".datasource-agent" / "plugins"
    if user_plugins_dir.exists():
        plugin_registry.discover_from_directory(user_plugins_dir, source="user")

    # Run parallel startup prefetches (ported from Claude Code's prefetch pattern)
    from src.services.prefetch import run_startup_prefetches
    prefetch_results = await run_startup_prefetches()
    logger.info(
        "startup.prefetch_complete",
        extra={"event": "startup.prefetch_complete", "results": prefetch_results},
    )

    logger.info("startup.ready", extra={"event": "startup.ready"})

    yield

    # Shutdown — terminate harness subprocesses FIRST (they outlive the event
    # loop otherwise, crawling and writing workspace files with no supervisor
    # attached), then close clients and the DB engine.
    from src.services.harness_orchestrator import shutdown_harness_processes
    try:
        await shutdown_harness_processes()
    except Exception:
        logger.warning("shutdown.harness_terminate_failed", exc_info=True)
    from src.services.cache import cache_service
    from src.services.mcp_client import mcp_client
    await mcp_client.disconnect_all()
    await cache_service.close()
    from src.db.engine import dispose_engine
    await dispose_engine()
    logger.info("Application shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="DataSource Discovery Agent",
        description="Unified Data Source Discovery Agent — 8-stage pipeline for finding APIs, files, and embedded data",
        version=SERVICE_VERSION,
        lifespan=lifespan,
    )

    # Request/response logging — register first so it wraps everything else.
    # NB: Starlette runs middlewares in reverse-registration order, so the
    # one added FIRST is the OUTERMOST. We want logging to be outermost so
    # it captures CORS preflight, validation errors, and downstream exceptions.
    app.add_middleware(RequestLoggingMiddleware)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.app.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Surface X-Query-Id so the browser can read it from the /discover
        # response when the call is cross-origin (direct-to-backend).
        expose_headers=["X-Query-Id"],
    )

    # Optional shared-secret gate. When APP_API_KEY is set, every request must
    # carry a matching `X-API-Key` header. Empty (default) = open, for the
    # single-user / localhost model (see SECURITY.md). OPTIONS (CORS preflight)
    # and /health are always allowed.
    @app.middleware("http")
    async def _api_key_guard(request: Request, call_next):
        key = settings.app.api_key
        if key and request.method != "OPTIONS" and not request.url.path.endswith("/health"):
            if request.headers.get("x-api-key") != key:
                return JSONResponse(
                    status_code=401, content={"detail": "missing or invalid X-API-Key"}
                )
        return await call_next(request)

    # Routes
    from src.api.routes.discover import router as discover_router
    from src.api.routes.harness import router as harness_router
    from src.api.routes.steer import router as steer_router
    from src.api.routes.stream import router as stream_router
    from src.api.routes.takeover import router as takeover_router
    app.include_router(discover_router)
    app.include_router(stream_router)
    app.include_router(steer_router)
    app.include_router(harness_router)
    app.include_router(takeover_router)

    # SSE-aware validation handler: when /discover rejects a query (markdown
    # fence, test envelope, low-content), the default JSONResponse breaks SSE
    # clients that assert Content-Type=text/event-stream before reading the
    # body. Emit a single SSE 'error' event with status_code so the harness
    # can capture the rejection through its normal SSE channel.
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        if request.url.path.endswith("/discover"):
            errors = exc.errors()
            messages = [str(e.get("msg", "")) for e in errors if e.get("msg")]
            payload = {
                "code": "validation_error",
                "status_code": 422,
                "message": "; ".join(messages) or "request validation failed",
                "errors": [
                    {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
                    for e in errors
                ],
            }

            async def _stream():
                yield sse_event("error", payload)

            return EventSourceResponse(_stream(), status_code=422)

        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    return app


app = create_app()
