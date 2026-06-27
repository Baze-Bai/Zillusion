"""TypeClassifier — 3-layer cascading source type classification.

Layer 1: URL pattern matching (free, <1ms)
Layer 2: HEAD request probing (cheap, ~100ms)
Layer 3: Haiku LLM batch classification (only uncertain URLs)
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from src.adapters.api_metadata import enrich_api_metadata
from src.classifiers.url_patterns import API_PATTERNS, DOMAIN_TYPE_MAP, FILE_PATTERNS
from src.config import settings
from src.models.candidates import SourceTypeClassification
from src.models.data_source import DataSourceType
from src.services.llm import llm_service
from src.tools.validation.head_prober import probe_url

logger = logging.getLogger(__name__)

_TYPE_MAP = {
    "api": DataSourceType.API,
    "file": DataSourceType.DOWNLOADABLE_FILE,
    "embedded": DataSourceType.EMBEDDED_DATA,
}


async def classify_source_types(
    urls_with_snippets: list[tuple[str, str]],
) -> list[SourceTypeClassification]:
    """Classify URLs into data source types using 3-layer cascading."""
    results: list[SourceTypeClassification] = []
    uncertain: list[tuple[str, str]] = []

    for url, snippet in urls_with_snippets:
        detected: list[DataSourceType] = []

        # === Layer 1: URL pattern matching (free, <1ms) ===
        if any(p.search(url) for p in FILE_PATTERNS):
            detected.append(DataSourceType.DOWNLOADABLE_FILE)

        if any(p.search(url) for p in API_PATTERNS):
            detected.append(DataSourceType.API)

        domain = urlparse(url).netloc.replace("www.", "").lower()
        for portal_domain, types in DOMAIN_TYPE_MAP.items():
            if portal_domain in domain:
                for t in types:
                    dtype = _TYPE_MAP.get(t)
                    if dtype and dtype not in detected:
                        detected.append(dtype)

        # Known API providers — bare hosts like fred.stlouisfed.org or
        # api.bls.gov don't carry /api/ URL hints (BLS's bare host even
        # 403s the HEAD probe), but the api_metadata registry maps them to
        # documented REST APIs we can backfill auth+docs for. Without this,
        # fred.stlouisfed.org gets HEAD-probed → text/html → classified as
        # embedded only and the api_spec the user came for never appears.
        if enrich_api_metadata(url) and DataSourceType.API not in detected:
            detected.append(DataSourceType.API)

        if detected:
            logger.debug("TypeClassifier L1 (pattern): %s → %s",
                          url[:60], [d.value for d in detected])
            results.append(SourceTypeClassification(
                url=url, detected_types=list(set(detected)),
                confidence=settings.classification.type_url_pattern_confidence, source="url_pattern",
            ))
            continue

        # === Layer 2: HEAD request probing (cheap, ~100ms) ===
        probe = await probe_url(url, timeout=settings.classification.type_head_timeout)
        logger.debug("TypeClassifier L2 (HEAD): %s → alive=%s, ct=%s, attach=%s",
                      url[:60], probe.is_alive, probe.content_type, probe.has_attachment)
        if probe.is_alive and probe.content_type:
            ct = probe.content_type.lower()
            file_indicators = [
                "text/csv", "application/json", "application/xml",
                "application/zip", "application/vnd.openxmlformats",
                "application/parquet", "application/octet-stream",
            ]
            if any(m in ct for m in file_indicators):
                detected.append(DataSourceType.DOWNLOADABLE_FILE)
            if probe.has_attachment:
                detected.append(DataSourceType.DOWNLOADABLE_FILE)
            if "text/html" in ct and not detected:
                detected.append(DataSourceType.EMBEDDED_DATA)

        if detected:
            logger.debug("TypeClassifier L2 result: %s → %s",
                          url[:60], [d.value for d in detected])
            results.append(SourceTypeClassification(
                url=url, detected_types=list(set(detected)),
                confidence=settings.classification.type_head_probe_confidence, source="head_probe",
            ))
            continue

        # === Layer 3: Collect for batch LLM classification ===
        logger.debug("TypeClassifier → uncertain (L3): %s", url[:60])
        uncertain.append((url, snippet))

    # Batch LLM classification for uncertain URLs
    if uncertain:
        logger.debug("TypeClassifier L3: %d uncertain URLs for LLM batch", len(uncertain))
        llm_results = await _batch_llm_classify(uncertain)
        for r in llm_results:
            logger.debug("TypeClassifier L3 result: %s → %s (conf=%.2f)",
                          r.url[:60], [t.value for t in r.detected_types], r.confidence)
        results.extend(llm_results)

    return results


async def _batch_llm_classify(
    urls_with_snippets: list[tuple[str, str]],
) -> list[SourceTypeClassification]:
    """Batch classify uncertain URLs using Haiku LLM."""
    items_text = "\n".join(
        f"[{i+1}] URL: {url}\n    Snippet: {snippet}"
        for i, (url, snippet) in enumerate(urls_with_snippets)
    )

    prompt = f"""Classify each URL into data source types. A URL can have MULTIPLE types.
Types: api (REST/GraphQL API), file (downloadable CSV/JSON/Excel/Parquet), embedded (data in webpage)

{items_text}

For each URL, output a JSON array:
[{{"index": 1, "types": ["api", "file"], "confidence": 0.8}}, ...]
Only include types you are confident about."""

    try:
        logger.debug("TypeClassifier L3 LLM prompt: %s", prompt[:500])
        response, _ = await llm_service.complete(
            messages=[{"role": "user", "content": prompt}],
            profile="type_classifier",
            model_tier="fast",
            max_tokens=settings.llm.max_tokens_classify,
        )
        logger.debug("TypeClassifier L3 LLM response: %s", response[:500])

        from src.utils.json_parse import extract_json
        parsed = extract_json(response, default=[])
        if not isinstance(parsed, list):
            logger.warning("TypeClassifier LLM: expected array, got %s", type(parsed).__name__)
            parsed = []

        results = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(urls_with_snippets):
                url = urls_with_snippets[idx][0]
                types = [_TYPE_MAP[t] for t in item.get("types", []) if t in _TYPE_MAP]
                if types:
                    results.append(SourceTypeClassification(
                        url=url,
                        detected_types=types,
                        confidence=item.get("confidence", settings.classification.type_llm_confidence),
                        source="llm",
                    ))

        return results

    except Exception as e:
        logger.warning("LLM type classification failed: %s", e)
        # Fallback: assume embedded for HTML pages
        return [
            SourceTypeClassification(
                url=url, detected_types=[DataSourceType.EMBEDDED_DATA],
                confidence=settings.classification.type_fallback_confidence, source="fallback",
            )
            for url, _ in urls_with_snippets
        ]
