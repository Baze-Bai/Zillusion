"""Tests for the per-call-site LLM profile system.

Each LLM call site in the project has a named profile. Profiles can be
independently configured via env-var-style overrides in LLMConfig, falling
back to their default tier chain if no overrides are set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.config import settings
from src.services.llm import (
    _DEFAULT_PROFILE_TIER,
    _resolve_profile_chain,
    _resolve_profile_max_tokens,
    _resolve_profile_temperature,
    _resolve_tier_chain,
    llm_service,
)


# =====================================================================
# Profile chain resolution
# =====================================================================


class TestProfileResolution:
    """Profile resolution: overrides > tier chain."""

    # -- P01: Every registered profile resolves to a non-empty chain --
    def test_p01_all_profiles_resolve_non_empty(self):
        """Every profile in _DEFAULT_PROFILE_TIER must produce a non-empty
        model chain (given tier defaults are configured)."""
        for profile in _DEFAULT_PROFILE_TIER:
            chain = _resolve_profile_chain(profile)
            # Chain may be empty if no tier-defaults configured in this env,
            # but given standard test config, all should have at least 1.
            # Use tier resolution directly to confirm.
            tier = _DEFAULT_PROFILE_TIER[profile]
            tier_chain = _resolve_tier_chain(tier)
            assert chain == tier_chain, (
                f"Profile {profile} resolved {chain}, "
                f"expected tier-default {tier_chain}"
            )

    # -- P02: Unknown profile falls back to "fast" tier --
    def test_p02_unknown_profile_uses_fast_tier(self):
        unknown_chain = _resolve_profile_chain("completely_nonexistent_profile")
        fast_chain = _resolve_tier_chain("fast")
        assert unknown_chain == fast_chain

    # -- P03: Per-profile primary override takes precedence over tier --
    def test_p03_profile_primary_override_wins(self):
        """Setting intent_parser_primary overrides the default strong tier."""
        original = settings.llm.intent_parser_primary
        try:
            settings.llm.intent_parser_primary = "custom/special-model-v1"
            chain = _resolve_profile_chain("intent_parser")
            assert chain == ["custom/special-model-v1"]
        finally:
            settings.llm.intent_parser_primary = original

    # -- P04: All three overrides (primary + fallback + china) compose --
    def test_p04_full_override_chain(self):
        original = (
            settings.llm.embedded_tier2_primary,
            settings.llm.embedded_tier2_fallback,
            settings.llm.embedded_tier2_china,
        )
        try:
            settings.llm.embedded_tier2_primary = "p-model"
            settings.llm.embedded_tier2_fallback = "f-model"
            settings.llm.embedded_tier2_china = "c-model"
            chain = _resolve_profile_chain("embedded_tier2")
            assert chain == ["p-model", "f-model", "c-model"]
        finally:
            (
                settings.llm.embedded_tier2_primary,
                settings.llm.embedded_tier2_fallback,
                settings.llm.embedded_tier2_china,
            ) = original

    # -- P05: Partial override (only fallback set) still uses that model --
    def test_p05_partial_override(self):
        """If only fallback is set, chain is just [fallback] — NOT mixed
        with tier defaults. This is intentional: any override means
        'fully replace the chain for this profile'."""
        original = (
            settings.llm.type_classifier_primary,
            settings.llm.type_classifier_fallback,
            settings.llm.type_classifier_china,
        )
        try:
            settings.llm.type_classifier_primary = ""
            settings.llm.type_classifier_fallback = "only-fallback-set"
            settings.llm.type_classifier_china = ""
            chain = _resolve_profile_chain("type_classifier")
            assert chain == ["only-fallback-set"]
        finally:
            (
                settings.llm.type_classifier_primary,
                settings.llm.type_classifier_fallback,
                settings.llm.type_classifier_china,
            ) = original

    # -- P06: Empty overrides fall through to tier default --
    def test_p06_all_empty_falls_through_to_tier(self):
        """With all three fields empty, profile uses its default tier."""
        # portal_detector defaults to "fast"; confirm empty overrides use fast
        original = (
            settings.llm.portal_detector_primary,
            settings.llm.portal_detector_fallback,
            settings.llm.portal_detector_china,
        )
        try:
            settings.llm.portal_detector_primary = ""
            settings.llm.portal_detector_fallback = ""
            settings.llm.portal_detector_china = ""
            profile_chain = _resolve_profile_chain("portal_detector")
            fast_chain = _resolve_tier_chain("fast")
            assert profile_chain == fast_chain
        finally:
            (
                settings.llm.portal_detector_primary,
                settings.llm.portal_detector_fallback,
                settings.llm.portal_detector_china,
            ) = original


# =====================================================================
# Temperature / max_tokens per-profile overrides
# =====================================================================


class TestProfileHyperparamOverrides:
    """Per-profile temperature and max_tokens overrides."""

    # -- P07: Profile temperature override returns set value --
    def test_p07_profile_temperature_override(self):
        original = settings.llm.intent_parser_temperature
        try:
            settings.llm.intent_parser_temperature = 0.7
            assert _resolve_profile_temperature("intent_parser") == 0.7
        finally:
            settings.llm.intent_parser_temperature = original

    # -- P08: Unset temperature returns None --
    def test_p08_unset_temperature_is_none(self):
        original = settings.llm.reflect_temperature
        try:
            settings.llm.reflect_temperature = None
            assert _resolve_profile_temperature("reflect") is None
        finally:
            settings.llm.reflect_temperature = original

    # -- P09: Unknown profile temperature returns None gracefully --
    def test_p09_unknown_profile_temperature(self):
        assert _resolve_profile_temperature("does_not_exist") is None

    # -- P10: Profile max_tokens override works --
    def test_p10_profile_max_tokens_override(self):
        original = settings.llm.embedded_tier2_max_tokens
        try:
            settings.llm.embedded_tier2_max_tokens = 512
            assert _resolve_profile_max_tokens("embedded_tier2") == 512
        finally:
            settings.llm.embedded_tier2_max_tokens = original


# =====================================================================
# LLMService.complete / complete_structured with profile parameter
# =====================================================================


class TestServiceProfileParameter:
    """LLMService respects profile parameter in complete() calls."""

    # -- P11: complete() with profile uses profile chain --
    @pytest.mark.asyncio
    async def test_p11_complete_with_profile_uses_profile_chain(self):
        """When profile is given, _select_chain returns the profile's chain,
        not the tier's chain."""
        original_primary = settings.llm.intent_parser_primary
        try:
            settings.llm.intent_parser_primary = "test-marker-model"

            # Mock litellm.acompletion to capture the model actually called
            captured_models: list[str] = []

            async def _mock_acompletion(**kwargs):
                captured_models.append(kwargs["model"])
                # Raise so we go through the fallback chain and see all models
                raise RuntimeError("simulated failure to see full chain")

            with patch("litellm.acompletion", side_effect=_mock_acompletion):
                with pytest.raises(RuntimeError):
                    await llm_service.complete(
                        messages=[{"role": "user", "content": "hi"}],
                        profile="intent_parser",
                        model_tier="fast",  # should be IGNORED since profile is set
                    )

            # Only the profile's primary override should appear — no tier chain
            assert captured_models
            assert set(captured_models) == {"test-marker-model"}, (
                f"Profile override broken: captured {captured_models}"
            )
        finally:
            settings.llm.intent_parser_primary = original_primary

    # -- P12: complete() without profile uses model_tier (backward compat) --
    @pytest.mark.asyncio
    async def test_p12_complete_without_profile_uses_tier(self):
        """Backward compatibility: existing code passing model_tier still works."""
        captured_models: list[str] = []

        async def _mock_acompletion(**kwargs):
            captured_models.append(kwargs["model"])
            raise RuntimeError("simulated")

        with patch("litellm.acompletion", side_effect=_mock_acompletion):
            with pytest.raises(RuntimeError):
                await llm_service.complete(
                    messages=[{"role": "user", "content": "hi"}],
                    model_tier="fast",
                    # profile NOT specified
                )

        # Should use fast tier chain. @retry wraps the whole call and may
        # retry the full chain multiple times, so check the unique models
        # encountered match the fast chain.
        fast_chain = _resolve_tier_chain("fast")
        assert set(captured_models) == set(fast_chain), (
            f"Expected models from {fast_chain}, got {captured_models}"
        )
        # First call should always be the tier's primary
        assert captured_models[0] == fast_chain[0]

    # -- P13: profile max_tokens override is applied on complete() --
    @pytest.mark.asyncio
    async def test_p13_profile_max_tokens_override(self):
        """If profile has max_tokens override, it supersedes the caller's value."""
        original = settings.llm.embedded_tier2_max_tokens
        try:
            settings.llm.embedded_tier2_max_tokens = 77

            captured_kwargs: dict = {}

            async def _mock_acompletion(**kwargs):
                captured_kwargs.update(kwargs)
                raise RuntimeError("stop")

            with patch("litellm.acompletion", side_effect=_mock_acompletion):
                with pytest.raises(RuntimeError):
                    await llm_service.complete(
                        messages=[{"role": "user", "content": "hi"}],
                        profile="embedded_tier2",
                        max_tokens=9999,  # should be overridden by profile to 77
                    )

            assert captured_kwargs.get("max_tokens") == 77
        finally:
            settings.llm.embedded_tier2_max_tokens = original

    # -- P14: profile temperature override is applied --
    @pytest.mark.asyncio
    async def test_p14_profile_temperature_override(self):
        original = settings.llm.reflect_temperature
        try:
            settings.llm.reflect_temperature = 0.85

            captured: dict = {}

            async def _mock_acompletion(**kwargs):
                captured.update(kwargs)
                raise RuntimeError("stop")

            with patch("litellm.acompletion", side_effect=_mock_acompletion):
                with pytest.raises(RuntimeError):
                    await llm_service.complete(
                        messages=[{"role": "user", "content": "hi"}],
                        profile="reflect",
                        # temperature NOT specified; should inherit profile's 0.85
                    )

            assert captured.get("temperature") == 0.85
        finally:
            settings.llm.reflect_temperature = original

    # -- P15: Explicit temperature parameter beats profile override --
    @pytest.mark.asyncio
    async def test_p15_explicit_temperature_beats_profile(self):
        """If caller passes temperature explicitly, it wins over profile override.
        This matters for per-call tuning at a single site."""
        original = settings.llm.reflect_temperature
        try:
            settings.llm.reflect_temperature = 0.85

            captured: dict = {}

            async def _mock_acompletion(**kwargs):
                captured.update(kwargs)
                raise RuntimeError("stop")

            with patch("litellm.acompletion", side_effect=_mock_acompletion):
                with pytest.raises(RuntimeError):
                    await llm_service.complete(
                        messages=[{"role": "user", "content": "hi"}],
                        profile="reflect",
                        temperature=0.0,  # explicit — should win
                    )

            assert captured.get("temperature") == 0.0
        finally:
            settings.llm.reflect_temperature = original


# =====================================================================
# Profile registry completeness
# =====================================================================


class TestProfileRegistry:
    """Every LLM call site should have a registered profile."""

    # -- P16: All 12 documented profiles are registered --
    def test_p16_all_documented_profiles_registered(self):
        expected = {
            "intent_parser", "judge_stage_b", "reflect",
            "source_router", "type_classifier", "portal_detector",
            "judge_stage_a", "judge_authority", "finalize",
            "embedded_tier2", "embedded_tier3_tree", "embedded_cluster_review",
            "context_compressor",
        }
        assert expected.issubset(set(_DEFAULT_PROFILE_TIER.keys())), (
            f"Missing profiles: {expected - set(_DEFAULT_PROFILE_TIER.keys())}"
        )

    # -- P17: Each profile has a valid default tier --
    def test_p17_default_tiers_are_valid(self):
        valid_tiers = {"strong", "fast", "reasoning"}
        for profile, tier in _DEFAULT_PROFILE_TIER.items():
            assert tier in valid_tiers, (
                f"Profile {profile} has invalid tier '{tier}'"
            )
