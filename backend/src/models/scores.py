"""Five-dimension scoring model for data source evaluation."""

from pydantic import BaseModel, Field, model_validator

from src.config import settings


def get_score_weights() -> dict[str, float]:
    """Get scoring weights from config, renormalized to sum=1.0 if any
    dimension is disabled via its enable_*_dim flag.

    2026-05-22: When ``enable_freshness_dim`` is False, freshness is
    excluded and the remaining 4 dimensions are scaled up proportionally
    so the weighted-sum range stays 0-10. This keeps the overall score
    comparable across config changes without re-tuning hand-picked
    weights manually.
    """
    s = settings.scoring
    weights = {
        "relevance": s.weight_relevance,
        "authority": s.weight_authority,
        "freshness": s.weight_freshness,
        "accessibility": s.weight_accessibility,
        "license_fit": s.weight_license_fit,
    }
    # Drop disabled dimensions and renormalize.
    if not s.enable_freshness_dim:
        weights.pop("freshness", None)
    total = sum(weights.values())
    if total > 0 and abs(total - 1.0) > 1e-9:
        # Only renormalize when we actually dropped something (saves a
        # noop pass when all dims enabled and total is already 1.0).
        weights = {k: v / total for k, v in weights.items()}
    return weights


def get_accessibility_scores() -> dict[str, float]:
    """Get accessibility score lookup from config."""
    s = settings.scoring
    return {
        "open": s.access_score_open,
        "free_reg": s.access_score_free_reg,
        "api_key_free": s.access_score_api_key_free,
        "oauth": s.access_score_oauth,
        "api_key_paid": s.access_score_api_key_paid,
        "paywall": s.access_score_paywall,
        "unknown": s.access_score_unknown,
    }


def get_freshness_half_life() -> dict[str, int]:
    """Get freshness half-life lookup from config."""
    s = settings.scoring
    return {
        "news": s.halflife_news,
        "finance": s.halflife_finance,
        "economics": s.halflife_economics,
        "market": s.halflife_market,
        "tech": s.halflife_tech,
        "science": s.halflife_science,
        "research": s.halflife_research,
        "health": s.halflife_health,
        "government": s.halflife_government,
        "geo": s.halflife_geo,
        "default": s.halflife_default,
    }


class SourceScores(BaseModel):
    """Multi-dimension evaluation scores for a data source.

    2026-05-22 (v3 redesign): added 4 new fit-dimension fields. Previously
    these were RELEVANCE MULTIPLIERS (0.4-1.0) applied inside the weighted
    sum; in the new architecture they are standalone 0-10 dimensions
    visible to the LLM ranker. The aggregate weighted-sum path still
    works for backward compat, but ``overall`` is now typically set by
    the LLM ranker rather than the weighted sum.
    """

    # Core 5 dimensions (existing) ──────────────────────────────────────
    relevance: float = Field(ge=0, le=10, description="LLM-scored with strict rubric (Stage-A)")
    authority: float = Field(ge=0, le=10, description="Hybrid: domain prior + metadata + LLM")
    freshness: float = Field(ge=0, le=10, description="Deterministic: exponential decay (may be disabled)")
    accessibility: float = Field(ge=0, le=10, description="Deterministic: access level lookup")
    license_fit: float = Field(ge=0, le=10, description="Rule-based + LLM-confirmed SPDX matching")

    # New fit dimensions (added 2026-05-22 v3) ─────────────────────────
    # Each is 0-10 (rescaled from the old 0.4-1.0 multiplier range).
    # Default 10.0 = "no penalty / fully applicable" so legacy code paths
    # that don't set them get a no-op effect.
    format_fit: float = Field(default=10.0, ge=0, le=10,
                              description="actionable spec for desired_formats")
    temporal_fit: float = Field(default=10.0, ge=0, le=10,
                                description="temporal coverage vs requested range")
    geographic_fit: float = Field(default=10.0, ge=0, le=10,
                                  description="geographic coverage vs requested scope")
    schema_coverage_fit: float = Field(default=10.0, ge=0, le=10,
                                       description="fields_present ∩ target_schema overlap")

    overall: float = Field(ge=-1, le=10, description="LLM ranker output or weighted aggregate, -1 = vetoed")

    # Rationales (existing + new) ───────────────────────────────────────
    relevance_rationale: str = ""
    authority_rationale: str = ""
    # New: filled by the LLM ranker for top-level explanation; concise
    rank_rationale: str = ""
    # New: filled when license veto path went through LLM confirmation
    license_rationale: str = ""

    @model_validator(mode="after")
    def _round_scores(self) -> "SourceScores":
        """Round float scores to 2 decimals to suppress IEEE-754 artifacts.

        Without this, intermediate multiplications (relevance×0.7 format
        penalty, weighted-sum aggregation) leak values like 2.0999999999996
        and 5.800000000000001 straight into the SSE payload. Rounding here
        keeps the model boundary clean regardless of which producer set
        the field. Preserve the -1 sentinel for vetoed overall.
        """
        self.relevance = round(self.relevance, 2)
        self.authority = round(self.authority, 2)
        self.freshness = round(self.freshness, 2)
        self.accessibility = round(self.accessibility, 2)
        self.license_fit = round(self.license_fit, 2)
        self.format_fit = round(self.format_fit, 2)
        self.temporal_fit = round(self.temporal_fit, 2)
        self.geographic_fit = round(self.geographic_fit, 2)
        self.schema_coverage_fit = round(self.schema_coverage_fit, 2)
        if self.overall != -1:
            self.overall = round(self.overall, 2)
        return self

    @staticmethod
    def aggregate(
        relevance: float,
        authority: float,
        freshness: float,
        accessibility: float,
        license_fit: float,
        is_retired: bool = False,
        is_llm_prior: bool = False,
    ) -> float:
        """Compute overall score with hard veto rules.

        ``is_llm_prior=True`` lowers the relevance veto floor to 3.0 — these
        sources were explicitly named by parse_intent in
        ``known_authoritative_sources`` and have already been vetted as
        authoritative for the broader query domain. Stage-B's strict
        relevance LLM occasionally drops them below the standard 4.0 veto
        for narrowly-worded queries (e.g. "USGS daily streamflow" scored
        NOAA National Water Model around 3.5 because it's a forecast model
        not a direct-observation feed, even though parse_intent listed
        NOAA NWM as one of the canonical authoritative sources). The
        aggregated score still reflects the LLM's strict relevance
        assessment so the source ranks lower than a perfect match — it
        just doesn't disappear from the final report entirely. A floor
        of 3.0 (rather than 2.0) ensures that sources the LLM judges as
        clearly off-topic (relevance ≤ 2, e.g. SEC EDGAR for an OHLCV
        price-data query) are still vetoed even when parse_intent
        speculatively listed them. The license veto stays absolute
        regardless: a license mismatch is actionable for the user and
        can't be overridden by parse_intent priors.
        """
        s = settings.scoring
        if s.veto_license_zero and license_fit == 0:
            return -1  # License doesn't meet constraint → eliminate
        veto_floor = 3.0 if is_llm_prior else s.veto_relevance_threshold
        if relevance < veto_floor:
            return -1  # Too low relevance → eliminate
        # 2026-05-22: weights dict may omit disabled dimensions (e.g.
        # freshness). Use .get(..., 0.0) so a dropped weight zeroes out
        # the term cleanly instead of raising KeyError.
        weights = get_score_weights()
        overall = (
            relevance * weights.get("relevance", 0.0)
            + authority * weights.get("authority", 0.0)
            + freshness * weights.get("freshness", 0.0)
            + accessibility * weights.get("accessibility", 0.0)
            + license_fit * weights.get("license_fit", 0.0)
        )
        if is_retired:
            overall = min(overall, s.retired_overall_cap)
        return overall
