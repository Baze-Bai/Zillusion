"""Verify DeepSeek V4 pricing was registered with litellm.

Also computes cost for a fake response shape so we can confirm
litellm.completion_cost() will actually return non-zero amounts after
registration.

Usage:
  python scripts/smoke_test_deepseek_pricing.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Triggers registration on import
import src.services.llm  # noqa: F401,E402
import litellm  # noqa: E402


def main() -> None:
    print("─── Registration check ───")
    for m in (
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
    ):
        known = m in litellm.model_cost
        marker = "✓" if known else "✗"
        print(f"  {marker} {m}: {'in model_cost' if known else 'MISSING'}")

    print("\n─── Pricing values ───")
    for m in ("deepseek-v4-pro", "deepseek-v4-flash"):
        e = litellm.model_cost.get(m, {})
        print(f"  {m}:")
        for key in (
            "input_cost_per_token",
            "input_cost_per_token_cache_hit",
            "cache_read_input_token_cost",
            "output_cost_per_token",
        ):
            v = e.get(key, "MISSING")
            if isinstance(v, float):
                per_m = v * 1_000_000
                print(f"    {key:35s}= {v:.4e}  (${per_m:.4f}/M)")
            else:
                print(f"    {key:35s}= {v}")

    print("\n─── completion_cost() simulation ───")
    # Simulate a typical agent-loop response: 10K input + 2K output
    fake_response = SimpleNamespace(
        model="deepseek-v4-pro",
        usage=SimpleNamespace(
            prompt_tokens=10_000,
            completion_tokens=2_000,
            total_tokens=12_000,
        ),
        choices=[],
    )
    try:
        cost = litellm.completion_cost(completion_response=fake_response)
        print(f"  10K in + 2K out on deepseek-v4-pro:")
        print(f"    cost = ${cost:.6f}")
        # Manual expected: 10000 * 4.35e-7 + 2000 * 8.7e-7
        expected = 10000 * 0.435 / 1_000_000 + 2000 * 0.87 / 1_000_000
        print(f"    expected = ${expected:.6f}  (10K*0.435/M + 2K*0.87/M)")
        ok = abs(cost - expected) < 1e-7
        print(f"    {'✓ matches' if ok else '✗ MISMATCH'}")
    except Exception as e:
        print(f"  ✗ completion_cost() raised: {type(e).__name__}: {e}")

    print("\n─── Estimated cost for a typical /discover query ───")
    # Rough breakdown based on hotel-5c run characteristics:
    #   parse_intent: 1 v4-pro call, ~3K in / 0.5K out
    #   agentic_discovery: ~60 turns, total ~250K in / ~25K out (heavy cache hit)
    #   judge stage_a: 1 v4-flash, ~3K in / 0.5K out
    #   judge stage_b: ~8 v4-pro calls, ~25K in / ~1.5K out total
    #   judge authority: ~8 v4-flash, ~5K in / ~1K out total
    #   reflect: 1 v4-pro, ~5K in / 1K out
    stages = [
        ("parse_intent (v4-pro)", "deepseek-v4-pro", 3_000, 500),
        ("agentic_discovery (v4-pro, ~50% cache hit)", "deepseek-v4-pro", 250_000, 25_000),
        ("judge stage_a (v4-flash)", "deepseek-v4-flash", 3_000, 500),
        ("judge stage_b 8x (v4-pro)", "deepseek-v4-pro", 25_000, 1_500),
        ("judge authority 8x (v4-flash)", "deepseek-v4-flash", 5_000, 1_000),
        ("reflect (v4-pro)", "deepseek-v4-pro", 5_000, 1_000),
    ]
    total = 0.0
    for label, model, p_tok, c_tok in stages:
        m = litellm.model_cost.get(model, {})
        cost = p_tok * m.get("input_cost_per_token", 0) + c_tok * m.get(
            "output_cost_per_token", 0
        )
        total += cost
        print(f"  {label:48s} ${cost:.4f}")
    print(f"  {'TOTAL (no cache discount)':48s} ${total:.4f}")
    print(f"\n  With 50% prompt cache hit rate on agent loop (typical):")
    # Recompute agent_loop with half input as cache hit
    half_miss_in = 125_000
    half_hit_in = 125_000
    out = 25_000
    m = litellm.model_cost.get("deepseek-v4-pro", {})
    cached_total = total - (
        250_000 * m.get("input_cost_per_token", 0) + 25_000 * m.get("output_cost_per_token", 0)
    ) + (
        half_miss_in * m.get("input_cost_per_token", 0)
        + half_hit_in * m.get("cache_read_input_token_cost", 0)
        + out * m.get("output_cost_per_token", 0)
    )
    print(f"  {'TOTAL with cache':48s} ${cached_total:.4f}")


if __name__ == "__main__":
    main()
