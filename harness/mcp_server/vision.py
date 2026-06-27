"""One-shot VLM probe: hand a screenshot + a question to a multimodal SUBMODEL
and get back a short TEXT answer — the eyes for a TEXT base model.

When the run's base model is text-only it cannot see images, so the
``browser_vision_probe`` tool (mcp_server/server.py) routes the current page's pixels
through a small multimodal submodel and returns only the answer as text. The
submodel is DECOUPLED from the main loop's model: it is configured separately
(``CRAWLER_EXPLORER_VISION_MODEL``) and reached through the SAME
``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` the harness already uses — either
direct Anthropic, or the LiteLLM proxy that translates the image block to
whatever backend the model name maps to. So a DeepSeek text run can borrow a
Gemini/Claude/GLM-V submodel as its "eyes" without the main loop paying
multimodal token costs every turn.

This is an EXPLORE-time aid only. Like ``browser_screenshot`` it cannot ride a
``deterministic`` route (the validator / production rerun workflow.py standalone,
with no LLM): anything it reveals must be turned into a deterministic mechanism
(a selector / JS read / OCR) or carried by the ``agentic`` route.
"""

from __future__ import annotations

import base64
import os
from typing import Any

# Default submodel: the cheapest multimodal Claude. Valid both direct
# (api.anthropic.com) and via the LiteLLM proxy (litellm_config.yaml maps the
# same friendly name). Override with CRAWLER_EXPLORER_VISION_MODEL to point at a
# Gemini / GPT-4o / GLM-V backend your ANTHROPIC_BASE_URL actually serves.
DEFAULT_SUBMODEL = "claude-haiku-4-5"

_DEFAULT_QUESTION = (
    "Describe what is visible on this page screenshot that is relevant to "
    "finding or extracting the target data — layout, any text baked into "
    "images, charts/canvas content, and any overlay/login/captcha state."
)


def submodel() -> str:
    return os.environ.get("CRAWLER_EXPLORER_VISION_MODEL", DEFAULT_SUBMODEL).strip() or DEFAULT_SUBMODEL


async def describe(
    image: bytes, image_format: str, question: str, *, max_tokens: int = 1024
) -> dict[str, Any]:
    """Ask the multimodal submodel ``question`` about ``image`` (png/jpeg bytes);
    return ``{ok, answer, submodel, image_format}`` or ``{ok: False, reason}``.

    Degrades gracefully (never raises): a missing SDK, missing API key, or a
    submodel the backend doesn't serve all return a text reason the agent can act
    on, rather than crashing the run.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return {"ok": False, "reason": "anthropic SDK not installed — the vision submodel is unavailable."}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "ok": False,
            "reason": "ANTHROPIC_API_KEY is unset — no backend for the vision submodel. "
            "browser_vision_probe borrows a multimodal model via ANTHROPIC_BASE_URL; set the key "
            "(and CRAWLER_EXPLORER_VISION_MODEL if the default isn't served).",
        }
    model = submodel()
    media_type = "image/png" if image_format == "png" else "image/jpeg"
    b64 = base64.standard_b64encode(image).decode("ascii")
    client = AsyncAnthropic()  # api_key + base_url from env
    try:
        msg = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {"type": "text", "text": question or _DEFAULT_QUESTION},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 — surface as a tool result, never crash the run
        return {
            "ok": False,
            "reason": (
                f"vision submodel call failed ({type(exc).__name__}: {exc}). Check that "
                f"CRAWLER_EXPLORER_VISION_MODEL='{model}' is served by your ANTHROPIC_BASE_URL."
            ),
        }
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    text = "".join(
        getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()
    return {"ok": True, "answer": text, "submodel": model, "image_format": image_format}
