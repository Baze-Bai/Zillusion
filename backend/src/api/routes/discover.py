"""Discovery endpoint — the main entry point for data source discovery.

POST /api/v1/discover validates a query, creates a persisted session, starts
the discovery run as a DETACHED background task (so it survives the request /
tab close), and returns an SSE stream subscribed to that run. The same stream
is also reachable via GET /api/v1/stream/{query_id} for reconnect + review.

The run body itself lives in ``src.agents.run_executor``; the live fan-out +
persistence layer is ``src.services.run_registry`` / ``event_store``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from src.agents.run_executor import execute_discovery_run
from src.api.routes.stream import event_stream
from src.config import settings
from src.services.event_store import create_session
from src.services.run_registry import run_registry
from src.utils.query_validator import validate_query_content
from src.utils.request_context import get_request_id, short_query_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["discovery"])

# Placeholder sidebar title length until parse_intent refreshes it.
_PLACEHOLDER_TITLE_CHARS = 60


class DiscoverRequest(BaseModel):
    query: str = Field(min_length=settings.budget.query_min_length, max_length=settings.budget.query_max_length)
    license_constraint: str = "any"
    budget_constraint: str = "any"
    geographic_scope: list[str] | None = None
    temporal_range: str | None = None
    desired_formats: list[str] | None = None
    max_iterations: int = Field(default=settings.budget.max_iterations, ge=1, le=settings.budget.max_iterations_upper_bound)
    # Eval / experiment correlation fields. All optional, surface unchanged
    # into run_config so cross-run analytics can group + compare without
    # re-deriving identity from free-form query text.
    experiment_tag: str | None = Field(
        default=None, max_length=120,
        description="Free-form tag for A/B grouping (e.g. 'baseline', 'tree-judge-v3').",
    )
    baseline_run_ref: str | None = Field(
        default=None, max_length=120,
        description="Prior query_id this run should be compared against (golden eval).",
    )
    expected_outcome: str | None = Field(
        default=None, max_length=600,
        description="Eval expectation (e.g. 'sufficient', 'n_sources>=10', 'must include nominatim').",
    )

    @field_validator("query")
    @classmethod
    def _reject_garbage_queries(cls, v: str) -> str:
        return validate_query_content(v)


@router.post("/discover")
async def discover(request: DiscoverRequest):
    """Submit a discovery query; start a detached run and return its SSE stream."""
    query_id = short_query_id()
    # request_id was minted by RequestLoggingMiddleware and is already in the
    # contextvar — capture it so the run + every SSE event carry the same key.
    request_id = get_request_id()
    logger.info(
        "discover.accepted",
        extra={
            "event": "discover.accepted",
            "query_preview": request.query[:100],
            "max_iterations": request.max_iterations,
            "license": request.license_constraint,
            "budget_constraint": request.budget_constraint,
            "geographic_scope": request.geographic_scope,
            "temporal_range": request.temporal_range,
            "desired_formats": request.desired_formats,
            "query_id": query_id,
        },
    )

    # Persist the session up-front (placeholder title) so the sidebar shows it
    # immediately; the run refreshes the title from parse_intent's output.
    placeholder_title = request.query.strip()[:_PLACEHOLDER_TITLE_CHARS] or "Untitled task"
    await create_session(
        query_id,
        query_text=request.query,
        title=placeholder_title,
        request_id=request_id,
        run_config={
            "max_iterations": request.max_iterations,
            "license_constraint": request.license_constraint,
            "budget_constraint": request.budget_constraint,
            "geographic_scope": request.geographic_scope,
            "temporal_range": request.temporal_range,
            "desired_formats": request.desired_formats,
            "experiment_tag": request.experiment_tag,
            "baseline_run_ref": request.baseline_run_ref,
            "expected_outcome": request.expected_outcome,
        },
        status="running",
    )

    # Start the detached run. Closing this connection no longer stops it.
    await run_registry.start(
        query_id,
        lambda h: execute_discovery_run(h, request, request_id, query_id),
    )

    # Return an SSE stream subscribed to the run (replay-then-live). Same
    # generator as GET /stream/{query_id}, so the POST contract is preserved.
    # The X-Query-Id header lets the conversational UI learn the id from the
    # response headers (before reading any body) and then attach via
    # GET /stream/{query_id} — so it can navigate to /c/{id} immediately.
    return EventSourceResponse(
        event_stream(query_id),
        headers={"X-Query-Id": query_id},
    )


@router.get("/health")
async def health():
    """Health check endpoint."""
    from src.main import SERVICE_VERSION
    return {
        "status": "ok",
        "service": "datasource-discovery-agent",
        "version": SERVICE_VERSION,
    }
