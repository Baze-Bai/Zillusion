"""Authority scoring — 3-layer hybrid.

Layer 1: Domain prior table (500+ entries, deterministic)
Layer 2: Metadata signal adjustments (citations/downloads/stars → ±2)
Layer 3: Haiku LLM fallback (only for unknown domains, metadata only)
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from src.config import settings
from src.judging.domain_prior_table import get_domain_authority
from src.services.llm import llm_service

logger = logging.getLogger(__name__)


def _as_num(v) -> float:
    """Coerce a metadata signal to a number; non-numeric (None, '1,200',
    'unknown') → 0.0 so a stray string can't raise TypeError in the threshold
    comparisons below (metadata is free-form, populated by many adapters)."""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").strip())
        except ValueError:
            return 0.0
    return 0.0


def _metadata_adjustment(metadata: dict) -> float:
    """Layer 2: Adjust authority based on metadata signals. Returns bounded adjustment."""
    s = settings.scoring
    adj = 0.0

    # Citation count
    citations = _as_num(metadata.get("citations", metadata.get("citation_count", 0)))
    if citations > s.authority_citation_high:
        adj += 1.5
    elif citations > s.authority_citation_medium:
        adj += 1.0
    elif citations > s.authority_citation_low:
        adj += 0.5

    # Download count
    downloads = _as_num(metadata.get("downloads", metadata.get("download_count", 0)))
    if downloads > s.authority_download_high:
        adj += 1.0
    elif downloads > s.authority_download_medium:
        adj += 0.5

    # GitHub stars
    stars = _as_num(metadata.get("stars", metadata.get("stargazers_count", 0)))
    if stars > s.authority_star_high:
        adj += 1.0
    elif stars > s.authority_star_medium:
        adj += 0.5

    # Negative signals
    if metadata.get("is_archived") or metadata.get("deprecated"):
        adj += s.authority_archived_penalty
    if metadata.get("has_issues_reported"):
        adj += s.authority_issues_penalty

    return max(s.authority_adjustment_min, min(s.authority_adjustment_max, adj))


async def score_authority(
    url: str,
    metadata: dict,
    provider: str = "",
    is_known_authoritative: bool = False,
) -> tuple[float, str]:
    """Compute authority score using 3-layer approach.

    When ``is_known_authoritative`` is True (caller signals that
    parse_intent vetted this source by name in
    ``known_authoritative_sources``), unknown-domain sources get a 7.0
    floor and a "Recognized authoritative publisher (vetted by
    parse_intent)" rationale instead of the misleading
    "Unrecognized publisher (.gov/.edu +2.0 TLD bonus)" string. Without
    this, state-agency .gov hosts that aren't in the static domain prior
    table (cwcb.colorado.gov, dwr.colorado.gov, water.ca.gov, ...) score
    only ~5.0 and look "Unrecognized" in the report, contradicting
    parse_intent's explicit recommendation. Layer 1 hits (sources already
    in the domain prior table) are unaffected — that table is more
    accurate than a fixed floor.

    Returns (score: 0-10, rationale: str).
    """
    domain = urlparse(url).netloc.replace("www.", "").lower()

    # === Layer 1: Domain prior table ===
    prior = get_domain_authority(domain)
    if prior is not None:
        # Apply Layer 2 adjustments
        adj = _metadata_adjustment(metadata)
        final = max(0, min(10, prior + adj))
        if adj > 0:
            rationale = f"Recognized publisher ({domain}) with strong metadata signals"
        elif adj < 0:
            rationale = f"Recognized publisher ({domain}); some quality signals are weak"
        else:
            rationale = f"Recognized publisher ({domain})"
        return round(final, 2), rationale

    # === Layer 2: Metadata-only scoring for unknown domains ===
    s = settings.scoring
    adj = _metadata_adjustment(metadata)
    base_unknown = s.authority_unknown_base + adj

    # Quick heuristics for unknown domains
    tld_bonus_label: str | None = None
    if ".gov" in domain or ".edu" in domain:
        base_unknown = min(10, base_unknown + s.authority_gov_edu_bonus)
        tld_bonus_label = f".gov/.edu +{s.authority_gov_edu_bonus:.1f}"
    elif ".org" in domain:
        base_unknown = min(10, base_unknown + s.authority_org_bonus)
        tld_bonus_label = f".org +{s.authority_org_bonus:.1f}"

    # known_authoritative floor (only when Layer 1 missed): parse_intent
    # vetted this provider by name, so the score should reflect that
    # vetting rather than the generic "+2.0 TLD bonus" treatment for
    # unknown .gov/.edu hosts. Skip Layer 3 LLM fallback as well — it has
    # no metadata to assess against and would just waste a Haiku call.
    if is_known_authoritative:
        floored = max(7.0, base_unknown)
        rationale = (
            f"Recognized authoritative publisher ({domain}); "
            f"vetted by parse_intent as a known authoritative source"
        )
        if adj > 0:
            rationale += " with positive metadata signals"
        elif adj < 0:
            rationale += " (some metadata signals are weak)"
        return round(min(10, floored), 2), rationale

    # === Layer 3: LLM fallback for remaining uncertainty ===
    # Disabled by default (2026-05-22). Empirical analysis on hotel-bd:
    # 24 calls → 12 responses → 11/12 returned score=7.0 (prompt anchor bias
    # on the "{score: 7.0}" example). The in-line LLM was a 7.0 echo machine
    # for commercial unknown domains, contributing almost no signal. The
    # raised ``authority_unknown_base`` (5.0 → 6.0) absorbs the equivalent
    # bump deterministically.
    #
    # To restore the legacy LLM-tiebreak: SCORING_AUTHORITY_ONLINE_LLM_ENABLED=true.
    # Long-term fix: replace with offline curation pipeline that pre-populates
    # ``domain_prior_table`` from a daily LLM-driven batch over unknown domains.
    if (
        s.authority_online_llm_enabled
        and s.authority_llm_fallback_min < base_unknown < s.authority_llm_fallback_max
    ):
        try:
            prompt = f"""Rate the authority/trustworthiness of this data source provider on 0-10 scale.
Provider: {provider or domain}
Metadata: {str(metadata)[:300]}
Domain type: {domain.split('.')[-1]}

IMPORTANT: Only assess based on the metadata provided. Do NOT infer from URL or domain name.
Output JSON: {{"score": <0-10 float>, "rationale": "..."}}"""

            response, _ = await llm_service.complete(
                messages=[{"role": "user", "content": prompt}],
                profile="judge_authority",
                model_tier="fast",
                max_tokens=settings.llm.max_tokens_authority,
            )
            from src.utils.json_parse import extract_json
            result = extract_json(response, default={})
            if not isinstance(result, dict):
                result = {}
            llm_rationale = str(result.get("rationale", "")).strip()
            if "score" in result and llm_rationale:
                llm_score = float(result["score"])
                return round(max(0, min(10, llm_score)), 2), f"LLM: {llm_rationale}"
            # LLM returned no usable rationale — fall through to deterministic.

        except Exception as e:
            logger.debug("Authority LLM fallback failed: %s", e)

    if adj > 0:
        rationale = f"Unrecognized publisher ({domain}); metadata signals are positive"
    elif adj < 0:
        rationale = f"Unrecognized publisher ({domain}); metadata signals are negative"
    else:
        rationale = f"Unrecognized publisher ({domain}); limited authority signals"
    if tld_bonus_label:
        rationale = f"{rationale} ({tld_bonus_label} TLD bonus)"
    return round(max(0, min(10, base_unknown)), 2), rationale
