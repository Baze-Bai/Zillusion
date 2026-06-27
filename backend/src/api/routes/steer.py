"""Turn-level steering for a live discovery run.

POST /api/v1/discover/{query_id}/steer pushes a user message into the live
agent's streaming-input queue; the agent incorporates it on its next turn. The
guidance is also published into the transcript (``user_steer``) so it shows in
the chat and is persisted for review.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.services.run_registry import run_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["steering"])


class SteerRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    # "note" → free-form guidance; "task_description_edit" → asks the agent to
    # update its task_description.md to reflect the guidance.
    kind: str = Field(default="note")


@router.post("/discover/{query_id}/steer")
async def steer(query_id: str, body: SteerRequest):
    """User feedback for a discovery run. Dispatch by run state:

      - run **LIVE** → push into the live agent's streaming-input; it incorporates
        the guidance on its next turn.
      - run **terminal / idle** (done) → RE-RUN discovery carrying the feedback
        (query = original + feedback), continuing the same session timeline."""
    handle = run_registry.get(query_id)

    # ── LIVE: steer the running agent ──
    if handle is not None and handle.status == "running":
        if body.kind == "task_description_edit":
            message = (
                "User guidance — update your task_description.md to reflect this and "
                "continue your discovery accordingly:\n" + body.content
            )
        else:
            message = "User guidance: " + body.content
        if not handle.push_steer(message):
            raise HTTPException(status_code=409, detail="run is not accepting input")
        seq = await handle.publish(
            "user_steer",
            {"query_id": query_id, "kind": body.kind, "content": body.content},
        )
        logger.info(
            "discover.steered",
            extra={"event": "discover.steered", "query_id": query_id, "kind": body.kind},
        )
        return {"accepted": True, "applies": "live", "seq": seq}

    # ── TERMINAL / idle: re-run discovery with the feedback ──
    from src.agents.run_executor import restart_discovery_run

    new_qid = await restart_discovery_run(query_id, body.content)
    if new_qid is None:
        raise HTTPException(
            status_code=409, detail="run not live and could not re-run this session"
        )
    new_handle = run_registry.get(new_qid)
    if new_handle is not None:
        await new_handle.publish(
            "user_steer",
            {"query_id": query_id, "kind": body.kind, "content": body.content},
        )
    logger.info(
        "discover.feedback_rerun",
        extra={"event": "discover.feedback_rerun", "query_id": query_id, "kind": body.kind},
    )
    return {"accepted": True, "applies": "reran", "restarted": True}
