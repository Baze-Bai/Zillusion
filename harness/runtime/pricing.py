"""DeepSeek cost recomputation for harness agent runs.

The Claude Agent SDK fills ``ResultMessage.total_cost_usd`` from CLAUDE's price
table even when the agent is routed to DeepSeek via ``ANTHROPIC_BASE_URL``. The
dominant token category in an agentic loop is ``cache_read`` (tens of millions of
tokens); DeepSeek bills a cache hit at $0.003625/1M while Claude's table prices
it ~10-80x higher, so the SDK overstates a DeepSeek run's cost ~25-40x (verified
on real run logs). When the run is on DeepSeek we therefore IGNORE the SDK's
``total_cost_usd`` and recompute from the SDK's own (authoritative, cumulative)
token ``usage`` with DeepSeek's published rates.

Rates mirror ``backend/src/services/llm.py``'s litellm registration — the harness
is a separate venv/process and can't import it, so the two must be kept in sync.
Per 1M tokens (USD):
    pro:   miss 0.435  / cache-hit 0.003625 / output 0.87
    flash: miss 0.14   / cache-hit 0.0028   / output 0.28
DeepSeek does not bill cache *writes* (``cache_creation_input_tokens``), so those
are ignored.
"""

from __future__ import annotations

import os

# Per-token rates (USD) = published per-1M price / 1e6.
_RATES: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"miss": 0.435 / 1e6, "hit": 0.003625 / 1e6, "out": 0.87 / 1e6},
    "deepseek-v4-flash": {"miss": 0.14 / 1e6, "hit": 0.0028 / 1e6, "out": 0.28 / 1e6},
}

# When the model isn't an explicitly-known DeepSeek model but the agent IS routed
# to DeepSeek (ANTHROPIC_BASE_URL), price the aggregate usage at the MAIN model's
# rate. The harness main agent runs on -pro; the SDK's small/fast model (-flash)
# only handles minor internal tasks, so pricing everything at -pro is a small
# CONSERVATIVE overestimate (pro > flash) — still ~30x closer than the SDK value.
_DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"


def _normalize(model: str | None) -> str:
    if not model:
        return ""
    return (model.split("/", 1)[1] if "/" in model else model).strip().lower()


def _resolve(model: str | None) -> str:
    name = _normalize(model)
    if name in _RATES:
        return name
    if "deepseek" in os.environ.get("ANTHROPIC_BASE_URL", "").lower():
        return _DEFAULT_DEEPSEEK_MODEL
    return ""  # not a DeepSeek run → caller keeps the SDK's own cost


def cost_from_usage(usage: dict | None, model: str | None = None) -> float | None:
    """DeepSeek-priced cost from an SDK ``usage`` dict, or ``None`` when this is
    not a DeepSeek run (caller keeps the SDK value) or ``usage`` is empty.

    ``usage`` keys (Anthropic/SDK form): ``input_tokens`` (cache MISS / non-cached
    input), ``cache_read_input_tokens`` (cache HIT), ``cache_creation_input_tokens``
    (not billed by DeepSeek), ``output_tokens``."""
    name = _resolve(model)
    if not name or not usage:
        return None
    r = _RATES[name]
    miss = usage.get("input_tokens") or 0
    hit = usage.get("cache_read_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    return miss * r["miss"] + hit * r["hit"] + out * r["out"]


def effective_cost(
    usage: dict | None, sdk_cost: float | None, model: str | None = None
) -> float | None:
    """The DeepSeek-recomputed cost when this is a DeepSeek run, else the SDK's
    own ``total_cost_usd`` unchanged."""
    recomputed = cost_from_usage(usage, model)
    return recomputed if recomputed is not None else sdk_cost
