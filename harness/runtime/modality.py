"""Base-model MODE resolution — text vs multimodal — and the browser-vision
tool surface that rides on it.

The harness runs its agentic loop on one of two BASE-MODEL MODES:

  * text       — a pure-text model (the default; e.g. DeepSeek-V4 routed via
                 ``ANTHROPIC_BASE_URL``). It cannot see images, so the browser
                 screenshot tool is WITHHELD from its tool surface.
  * multimodal — a vision-capable model (Claude, Gemini, GPT-4o/4.1/5, GLM-4.x-V,
                 Qwen-VL, …). It gains an EXTRA information source: it can call
                 ``browser_screenshot`` to SEE the live page as an image, beside
                 the DOM/HTML it already reads with ``browser_content`` /
                 ``browser_player``.

Which mode a run uses is resolved by :func:`resolve_vision`:

  1. **Explicit override** — env ``CRAWLER_EXPLORER_VISION``
     (``1``/``true``/``on`` → vision, ``0``/``false``/``off`` → text). The CLI's
     ``--vision on|off`` sets this; the orchestrator forwards the RESOLVED value
     to the MCP server so its ``browser_screenshot`` backstop agrees with the
     tool gate the agent sees.
  2. **Model registry** — match the (litellm-friendly or raw provider) model
     name against known vision / text-only families.
  3. **Default — text.** Vision is only turned ON when we positively know the
     model supports it; a sketchy guess that ships image blocks to a text model
     just burns tokens or 400s at the provider.

This mirrors ``runtime/pricing.py``: the harness is a separate venv/process from
the backend, so the modality knowledge is duplicated here rather than imported,
and the two must be kept in sync when models change.
"""

from __future__ import annotations

import os

# ── the per-mode vision tool surface ────────────────────────────────────────
# Bare tool names exactly as the MCP server registers them. Qualify with the
# server key for allow/disallow lists via :func:`qualified`. The two modes get
# DIFFERENT, mutually-exclusive vision tools:
#   * MULTIMODAL base model → browser_screenshot (it SEES the page directly).
#   * TEXT base model       → browser_vision_probe (a multimodal SUBMODEL is its eyes:
#                             screenshot → one-shot VLM call → short text answer).
VISION_TOOLS: tuple[str, ...] = ("browser_screenshot",)
TEXT_VISION_TOOLS: tuple[str, ...] = ("browser_vision_probe",)
# Every vision tool, for blanket removal (e.g. API workflows have no browser).
ALL_VISION_TOOLS: tuple[str, ...] = (*VISION_TOOLS, *TEXT_VISION_TOOLS)

# .mcp.json registers the browser server under this key; tool names the agent
# (and allow/disallow lists) see are ``mcp__<key>__<tool>``.
MCP_SERVER_KEY = "browser-harness"

# The env var that carries the RESOLVED mode to the MCP server subprocess (its
# browser_screenshot backstop reads it) and doubles as the explicit override.
VISION_ENV = "CRAWLER_EXPLORER_VISION"


# ── model-name → modality registry ──────────────────────────────────────────
# Matched case-insensitively as SUBSTRINGS of the resolved model name, so both
# litellm-friendly names (``gemini-2.5-pro``, ``gpt-5``, ``glm-4.5v``) and raw
# provider ids (``anthropic/claude-...``, ``gemini/gemini-...``) hit.
_VISION_FRAGMENTS: tuple[str, ...] = (
    "claude",  # every current Claude model is multimodal
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "o4-",
    "omni",
    "gemini",  # Gemini 1.5 / 2.x are multimodal
    "glm-4v",
    "glm-4.5v",
    "glm-4.6v",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "-vl",  # generic vision-language suffix (pixtral-style families)
    "vision",
    "pixtral",
    "llava",
    "internvl",
)

# UNAMBIGUOUS vision suffixes — these mean vision-language regardless of any
# text-family prefix, so they're checked BEFORE the text list. Without this,
# "deepseek-vl" / "qwen2.5-coder-vl" / "llama-3.2-vision" would be force-matched
# to text by their family prefix and wrongly denied the screenshot tool.
_STRONG_VISION_FRAGMENTS: tuple[str, ...] = ("-vl", "vision")

# Known TEXT-ONLY families. Checked before the (looser) vision list so a coder
# model whose name brushes a fragment isn't misclassified — but AFTER the strong
# vision suffixes above, which override a text-family prefix.
_TEXT_FRAGMENTS: tuple[str, ...] = (
    "deepseek-v",  # deepseek-v4-pro / -flash / -v3 — text (NOT deepseek-vl, see strong list)
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-coder",
    "deepseek-r1",
    "qwen2.5-coder",
    "qwen-coder",
    "llama3.1",  # text-only Llama 3.1 (3.2-vision is caught by the strong list)
)


def _normalize(model: str | None) -> str:
    return (model or "").strip().lower()


def _env_override() -> bool | None:
    """The explicit ``CRAWLER_EXPLORER_VISION`` decision, or None when unset /
    unrecognised (fall through to the registry)."""
    raw = os.environ.get(VISION_ENV)
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("1", "true", "on", "yes", "vision", "multimodal"):
        return True
    if v in ("0", "false", "off", "no", "text"):
        return False
    return None


def _registry_modality(model: str | None) -> bool | None:
    """Modality from the model name alone, or None when the name carries no
    signal (caller falls back to the text default)."""
    name = _normalize(model)
    if not name:
        # No explicit model id. If we're routed at a known text backend, that's
        # the signal (mirrors pricing.py's ANTHROPIC_BASE_URL sniff).
        if "deepseek" in os.environ.get("ANTHROPIC_BASE_URL", "").lower():
            return False
        return None
    # Strong vision suffix wins over a text-family prefix (deepseek-vl, etc.).
    if any(frag in name for frag in _STRONG_VISION_FRAGMENTS):
        return True
    if any(frag in name for frag in _TEXT_FRAGMENTS):
        return False
    if any(frag in name for frag in _VISION_FRAGMENTS):
        return True
    return None


def resolve_vision(model: str | None = None) -> bool:
    """True when the base model is MULTIMODAL (browser screenshots are a usable
    information source), False for a pure-text base model.

    Precedence: explicit ``CRAWLER_EXPLORER_VISION`` override > model registry >
    text default.
    """
    override = _env_override()
    if override is not None:
        return override
    registry = _registry_modality(model)
    if registry is not None:
        return registry
    return False


def qualified(
    tools: tuple[str, ...] | list[str] = VISION_TOOLS, server: str = MCP_SERVER_KEY
) -> list[str]:
    """``mcp__<server>__<tool>`` names for allow_tools / disallowed_tools lists."""
    return [f"mcp__{server}__{t}" for t in tools]


def enabled_vision_tools(model: str | None = None) -> tuple[str, ...]:
    """The vision tool(s) this base-model mode SHOULD have (bare names):
    ``browser_screenshot`` for a multimodal model, ``browser_vision_probe`` for a text
    model. The other is withheld — they are mutually exclusive."""
    return VISION_TOOLS if resolve_vision(model) else TEXT_VISION_TOOLS


def gated_off_tools(model: str | None = None) -> list[str]:
    """The vision tool(s) to WITHHOLD from this mode (qualified ``mcp__…`` names),
    for a disallowed_tools list: the mode keeps its own vision tool and drops the
    other's. So a text model never sees ``browser_screenshot`` and a multimodal
    model never sees the (redundant) ``browser_vision_probe``."""
    keep = set(enabled_vision_tools(model))
    return qualified(tuple(t for t in ALL_VISION_TOOLS if t not in keep))


def vision_env(model: str | None = None) -> dict[str, str]:
    """Env to forward to the MCP server so its vision backstops agree with the
    agent-side gate. Always sets a concrete ``CRAWLER_EXPLORER_VISION`` ("1"/"0").

    For a TEXT run it ALSO forwards the submodel backend creds
    (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` / ``CRAWLER_EXPLORER_VISION_MODEL``)
    so ``browser_vision_probe``'s one-shot VLM call has a backend — stdio MCP servers do
    not reliably inherit the parent env, so we pass these explicitly rather than
    assume inheritance. Only values actually present are forwarded."""
    text_mode = not resolve_vision(model)
    env = {VISION_ENV: "0" if text_mode else "1"}
    if text_mode:
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CRAWLER_EXPLORER_VISION_MODEL"):
            val = os.environ.get(key)
            if val:
                env[key] = val
    return env
