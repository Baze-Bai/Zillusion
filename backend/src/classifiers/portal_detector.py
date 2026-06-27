"""Portal Detector — 3-layer cascading to identify data portals worth /map exploration.

Layer 1: Known portals whitelist (deterministic, zero cost, <1ms)
Layer 2: URL pattern + snippet heuristics (deterministic, zero cost, <1ms)
Layer 3: Haiku LLM fallback for uncertain URLs (~$0.002)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from src.classifiers.url_patterns import KNOWN_PORTALS, PORTAL_SIGNAL_WORDS, PORTAL_URL_PATTERNS
from src.config import settings
from src.services.llm import llm_service

logger = logging.getLogger(__name__)


@dataclass
class PortalDecision:
    url: str
    is_portal: bool
    confidence: float
    source: str  # whitelist / url_pattern / snippet_signal / llm


async def detect_portals(
    urls_with_snippets: list[tuple[str, str]],
) -> list[PortalDecision]:
    """Identify which URLs are data portals worth deep exploration."""
    results: list[PortalDecision] = []
    uncertain: list[tuple[str, str]] = []

    for url, snippet in urls_with_snippets:
        domain = urlparse(url).netloc.replace("www.", "").lower()

        # === Layer 1: Known portals whitelist ===
        if domain in KNOWN_PORTALS or any(kp in domain for kp in KNOWN_PORTALS):
            results.append(PortalDecision(
                url=url, is_portal=True, confidence=settings.classification.portal_whitelist_confidence, source="whitelist",
            ))
            continue

        # === Layer 2: URL pattern heuristics ===
        if any(p.search(url) for p in PORTAL_URL_PATTERNS):
            results.append(PortalDecision(
                url=url, is_portal=True, confidence=settings.classification.portal_url_pattern_confidence, source="url_pattern",
            ))
            continue

        if snippet and any(w in snippet.lower() for w in PORTAL_SIGNAL_WORDS):
            results.append(PortalDecision(
                url=url, is_portal=True, confidence=settings.classification.portal_snippet_confidence, source="snippet_signal",
            ))
            continue

        # === Layer 3: Uncertain → batch LLM ===
        uncertain.append((url, snippet))

    # LLM classification for uncertain URLs
    if uncertain:
        llm_decisions = await _batch_llm_portal_detect(uncertain)
        results.extend(llm_decisions)

    portal_count = sum(1 for r in results if r.is_portal)
    logger.info("Portal detection: %d/%d are portals", portal_count, len(results))
    return results


async def _batch_llm_portal_detect(
    urls_with_snippets: list[tuple[str, str]],
) -> list[PortalDecision]:
    """Batch detect portals using Haiku LLM."""
    items_text = "\n".join(
        f"[{i+1}] URL: {url}\n    Snippet: {snippet}"
        for i, (url, snippet) in enumerate(urls_with_snippets)
    )

    prompt = f"""Determine if each URL is a "data portal" — a site whose primary purpose is providing
searchable, downloadable, or API-accessible datasets (like data.gov, kaggle.com, etc.).

NOT portals: news sites, company homepages, blog posts, individual API docs, single-file downloads.

{items_text}

Output JSON array: [{{"index": 1, "is_portal": true, "confidence": 0.8}}, ...]"""

    try:
        response, _ = await llm_service.complete(
            messages=[{"role": "user", "content": prompt}],
            profile="portal_detector",
            model_tier="fast",
            max_tokens=settings.llm.max_tokens_portal_detect,
        )

        from src.utils.json_parse import extract_json
        parsed = extract_json(response, default=[])
        if not isinstance(parsed, list):
            logger.warning("PortalDetector LLM: expected array, got %s", type(parsed).__name__)
            parsed = []

        decisions = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(urls_with_snippets):
                decisions.append(PortalDecision(
                    url=urls_with_snippets[idx][0],
                    is_portal=item.get("is_portal", False),
                    confidence=item.get("confidence", 0.5),
                    source="llm",
                ))
        return decisions

    except Exception as e:
        logger.warning("LLM portal detection failed: %s", e)
        return [
            PortalDecision(url=url, is_portal=False, confidence=0.3, source="fallback")
            for url, _ in urls_with_snippets
        ]
