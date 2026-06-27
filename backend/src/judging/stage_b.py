"""Stage-B Judging: Sonnet 5-dimension scoring.

Full evaluation of top-30 survivors with:
- Relevance (LLM with rubric)
- Authority (domain prior + metadata + LLM fallback)
- Freshness (deterministic exponential decay)
- Accessibility (deterministic lookup)
- License fit (SPDX rules)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

from pydantic import BaseModel, Field

from src.config import settings
from src.judging.authority import score_authority
from src.judging.deterministic import (
    score_accessibility,
    score_format_fit,
    score_freshness,
    score_geographic_fit,
    score_license_fit,
    score_schema_coverage_fit,
    score_temporal_fit,
)
from src.judging.rubric import (
    _normalize_field_name,
    format_relevance_prompt,
    is_page_metadata_field,
)
from src.models.data_source import DataSource, DataSourceType
from src.models.requirement import StructuredRequirement
from src.models.scores import SourceScores
from src.services.llm import llm_service
from src.services.run_logger import log_event
from src.utils.json_parse import extract_json

logger = logging.getLogger(__name__)


class RelevanceOutput(BaseModel):
    """Structured output for relevance scoring."""
    score: float = Field(ge=0, le=10, description="Relevance score 0-10")
    rationale: str = Field(
        default="",
        description=(
            "2-4 short sentences. Cover which dimensions match, which do not, "
            "and one specific reason this beats a generic search result. "
            "Cite concrete facts from the source description."
        ),
    )


# Sentinel prefix used to mark relevance scores produced by the fallback path
# (LLM unavailable or unparseable). _score_single_source checks for this
# prefix before applying deterministic relevance penalties (format_fit /
# temporal_fit) — without the check, a Z.AI rate-limit storm reduces every
# scoring to the 5.0 fallback, then format_fit×0.7 drops it to 3.5 which
# trips the 4.0 veto threshold and silently kills the entire candidate set.
_SCORING_FAILED_PREFIX = "[scoring_failed] "


def _is_scoring_failed_rationale(rationale: str) -> bool:
    return bool(rationale) and rationale.startswith(_SCORING_FAILED_PREFIX)


async def _score_relevance(
    source: DataSource, requirement: StructuredRequirement
) -> tuple[float, str]:
    """Score relevance using strong model with strict rubric.

    Tries instructor structured output first (most reliable), falls back to
    robust JSON extraction on failure.
    """
    prompt = format_relevance_prompt(
        requirement=requirement.model_dump(),
        source=source.model_dump(),
    )
    logger.debug("Stage-B relevance prompt for %s: %s", source.name[:40], prompt[:300])

    # Floor max_tokens at the working minimum: the 3-point rationale rubric
    # ("MATCH / DO NOT MATCH / better-or-worse-than-Google") needs ≥1536 tokens
    # of headroom or the LLM truncates mid-sentence (observed: rationale cut at
    # "Type matches (API"). External LLM_MAX_TOKENS_STAGE_B env overrides
    # below this floor cause silent data loss in stored rationales.
    stage_b_tokens = max(settings.llm.max_tokens_stage_b, 1536)

    # Strategy 1: instructor structured output (preferred — automatic retries)
    try:
        result, _ = await llm_service.complete_structured(
            messages=[{"role": "user", "content": prompt}],
            response_model=RelevanceOutput,
            profile="judge_stage_b",
            model_tier="strong",
            max_tokens=stage_b_tokens,
        )
        logger.debug("Stage-B relevance (structured): %s → score=%.1f, rationale=%s",
                      source.name[:40], result.score, result.rationale[:100])
        return max(0.0, min(10.0, result.score)), _sanitize_rationale(result.rationale)
    except Exception as e:
        logger.debug("Structured relevance scoring failed for %s: %s, falling back", source.name[:40], e)

    # Strategy 2: robust JSON extraction from free-form response
    try:
        response, _ = await llm_service.complete(
            messages=[{"role": "user", "content": prompt}],
            profile="judge_stage_b",
            model_tier="strong",
            max_tokens=stage_b_tokens,
        )
        parsed = extract_json(response, default={})
        if parsed and "score" in parsed:
            score = float(parsed.get("score", 5.0))
            return max(0.0, min(10.0, score)), _sanitize_rationale(parsed.get("rationale", ""))
        logger.warning("No valid JSON in relevance response for %s: %s",
                       source.name[:40], response[:150])
        return 5.0, _SCORING_FAILED_PREFIX + "JSON parse failed"
    except Exception as e:
        logger.warning("Relevance scoring failed for %s: %s", source.name[:40], e)
        return 5.0, _SCORING_FAILED_PREFIX + "Scoring failed"


def _embedded_has_service_channel(source: DataSource) -> bool:
    """True when the source's JSON-LD exposes Schema.org ``availableChannel``
    entries that point to a real service endpoint (REST API / search service
    / sitemap) distinct from the page itself.

    Schema.org Service / DataCatalog pages publish their data-access points as
    ``availableChannel`` ServiceChannels rather than as a flat ``distribution``
    URL. HydroShare's homepage carries availableChannel entries pointing at
    ``/hsapi/resource`` (REST API) and ``/sitemap.xml`` (resource enumeration)
    — these IS the actionable spec. Without recognising them, the rubric
    tags the page as "no actionable spec", format_fit×0.7 slashes its
    relevance, and an authoritative data catalog falls below the veto.
    """
    raw = (source.metadata or {}).get("tier0_raw_extractions") or {}
    if not isinstance(raw, dict):
        return False
    page_url = (source.url or "").rstrip("/")
    for items in raw.get("json-ld") or []:
        nodes = items if isinstance(items, list) else [items]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            channels = node.get("availableChannel")
            if not channels:
                continue
            entries = channels if isinstance(channels, list) else [channels]
            for ch in entries:
                if not isinstance(ch, dict):
                    continue
                svc_url = ch.get("serviceUrl")
                if isinstance(svc_url, str) and svc_url.strip():
                    if svc_url.rstrip("/") != page_url:
                        return True
                # Some publishers nest the URL inside providesService.target
                provides = ch.get("providesService")
                if isinstance(provides, dict):
                    target = provides.get("potentialAction", {})
                    if isinstance(target, dict):
                        t = target.get("target")
                        if isinstance(t, str) and t.strip() and t.rstrip("/") != page_url:
                            return True
                        if isinstance(t, dict):
                            tmpl = t.get("urlTemplate") or t.get("url")
                            if isinstance(tmpl, str) and tmpl.strip() and tmpl.rstrip("/") != page_url:
                                return True
    return False


# OpenGraph / RDFa property URIs that carry the page's last-modification
# timestamp. Different extractors and different publishers use different
# URI flavors (http vs https, with or without article# fragment), so we
# probe all known variants and take the latest parseable date.
_RDFA_PAGE_DATE_KEYS: tuple[str, ...] = (
    "http://ogp.me/ns/article#modified_time",
    "https://ogp.me/ns/article#modified_time",
    "https://ogp.me/ns#updated_time",
    "http://ogp.me/ns#updated_time",
    "http://ogp.me/ns/article#published_time",
    "https://ogp.me/ns/article#published_time",
)

# Match the FIRST 4-digit year inside the requirement.temporal_range string.
# parse_intent emits formats like "2022-01-01 to 2023-12-31" / "2004-2024" /
# "2020-01-01/.." — taking the first year is sufficient for the start-year
# comparison this penalty needs.
_FIRST_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _embedded_page_last_modified(source: DataSource) -> datetime | None:
    """Return the embedded page's last-modified timestamp from RDFa
    OpenGraph metadata, or None when no parseable date is present.

    Probes ``article:modified_time`` / ``og:updated_time`` /
    ``article:published_time`` across the http/https URI flavors emitted by
    different RDFa extractors. Picks the most recent parseable date among
    them — pages that emit only a published_time still get a usable signal
    when modified_time is missing.
    """
    if source.source_type != DataSourceType.EMBEDDED_DATA:
        return None
    raw = (source.metadata or {}).get("tier0_raw_extractions") or {}
    if not isinstance(raw, dict):
        return None
    latest: datetime | None = None
    for items in (raw.get("rdfa") or []):
        nodes = items if isinstance(items, list) else [items]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key in _RDFA_PAGE_DATE_KEYS:
                vals = node.get(key)
                if not vals:
                    continue
                entries = vals if isinstance(vals, list) else [vals]
                for entry in entries:
                    val = None
                    if isinstance(entry, dict):
                        val = entry.get("@value")
                    elif isinstance(entry, str):
                        val = entry
                    if not isinstance(val, str) or not val.strip():
                        continue
                    parsed: datetime | None = None
                    try:
                        parsed = datetime.fromisoformat(val.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        try:
                            parsed = datetime.strptime(val[:10], "%Y-%m-%d")
                        except (ValueError, TypeError):
                            continue
                    if parsed is None:
                        continue
                    naive = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
                    if latest is None or naive > latest:
                        latest = naive
    return latest


def _score_stale_page_fit(
    source: DataSource, requirement: StructuredRequirement
) -> tuple[float, str | None]:
    """Return a relevance multiplier when the embedded page's RDFa-extracted
    last-modified timestamp predates the requested temporal range start by at
    least a year, signaling a stale archive page being scored against a
    fresh-period query.

    Catches the regression case where opendata.ecdc.europa.eu emitted RDFa
    metadata with modified_time=2020-12-14 (the page itself: "Download
    historical data (to 14 December 2020)") yet was scored top for a
    2022-2023 testing query — source.temporal_coverage carried the broad
    portal blurb "COVID-19 from 2020", which fooled score_temporal_fit into
    treating the page as covering the requested window.

    Returns (1.0, None) when no penalty applies; (0.5, reason) when the page
    is at least a year stale.
    """
    if source.source_type != DataSourceType.EMBEDDED_DATA:
        return 1.0, None
    if not requirement.temporal_range:
        return 1.0, None
    match = _FIRST_YEAR_RE.search(requirement.temporal_range)
    if not match:
        return 1.0, None
    req_start_year = int(match.group(1))
    last_modified = _embedded_page_last_modified(source)
    if last_modified is None:
        return 1.0, None
    if last_modified.year + 1 <= req_start_year:
        return 0.5, (
            f"page modified {last_modified.date().isoformat()} is "
            f"≥1y before requested range start {req_start_year} "
            f"— stale archive"
        )
    return 1.0, None


# Schema.org @type values whose presence on an embedded source means the
# page IS hosting a dataset description, regardless of whether its
# fields_present happens to be all schema.org property names that we strip
# elsewhere as page metadata. A HydroShare /resource/<UUID> page surfaces
# JSON-LD with @type=Dataset and properties [name, description, distribution,
# variableMeasured, spatialCoverage, temporalCoverage, license, ...] — every
# one of those property names is in _PAGE_METADATA_FIELDS (correct: the LLM
# rubric should not see them as data columns), so real_fields=[] and the
# old check returned False. format_fit×0.7 then dragged a typical 5.5
# relevance below the 4.0 veto and silently killed every HydroShare result.
# A page emitting Dataset/DataDownload/Datafeed JSON-LD IS the spec.
_DATASET_BEARING_SCHEMAS: frozenset[str] = frozenset({
    "dataset",
    "datadownload",
    "datasetseries",
    "scientificdataset",
    "datafeed",
})


# Schema.org @type values whose presence on an embedded source means the
# page is hosting CONTENT (news article, blog post, FAQ) rather than a
# dataset. These pages CAN'T serve structured data regardless of whether
# their host domain is otherwise authoritative — so they should be
# vetoed even when discovered through a parse_intent llm_prior seed.
# Regression case: data.europa.eu is a vetted catalog, but its homepage
# crawl extracted three news-article RDFa entries (World IP Day, Albania
# portal, AI Continent plan); fields_present=[schema.org/name,
# schema.org/text], schema_type=schema.org/Article. The LLM correctly
# scored relevance=3.0, but the llm_prior salvage floor of 3.0 (used to
# protect topically-narrow vetoes for vetted publishers) kept the
# article in the final report. Hard-veto these schema_types so a
# wrong-page crawl can't pollute the result via the salvage exemption.
_CONTENT_PAGE_SCHEMAS: frozenset[str] = frozenset({
    "article",
    "newsarticle",
    "blogposting",
    "socialmediaposting",
    "techarticle",
    "report",
    "scholarlyarticle",
    "webpage",
    "aboutpage",
    "contactpage",
    "checkoutpage",
    "faqpage",
    "qapage",
    "searchresultspage",
    "profilepage",
    "itempage",
    # Event listings (meeting agendas, conference pages) describe an
    # occurrence rather than a dataset — fields like eventStatus / startDate
    # / location / organizer have zero overlap with any data schema. A
    # vetted catalog (e.g. Western States Water Council homepage emits Event
    # JSON-LD for upcoming meetings) crawled into Event JSON-LD should be
    # filtered the same way an Article would, so the llm_prior salvage
    # exemption can't keep it ranked despite zero schema-fit.
    "event",
    "businessevent",
    "educationevent",
    "userinteraction",
})


def _embedded_is_content_page(source: DataSource) -> bool:
    """True when the embedded source's JSON-LD describes a content page
    (Article/News/Blog/WebPage) rather than a dataset.

    Checked AFTER format_fit softening but BEFORE aggregate so the
    llm_prior salvage exemption can be disabled for these instances —
    a parse_intent-vetted catalog whose specific crawled URL hit a news
    article should still get filtered, even though the catalog as a
    publisher is legitimate. The schema_type is normalized by stripping
    any URI prefix (e.g. ``http://schema.org/Article`` → ``article``)
    before lookup.
    """
    if source.source_type != DataSourceType.EMBEDDED_DATA:
        return False
    spec = source.embedded_spec
    if not spec:
        return False
    schema_norm = (spec.schema_type or "").strip().lower()
    if not schema_norm:
        return False
    if "/" in schema_norm:
        schema_norm = schema_norm.rsplit("/", 1)[-1]
    if "#" in schema_norm:
        schema_norm = schema_norm.rsplit("#", 1)[-1]
    return schema_norm in _CONTENT_PAGE_SCHEMAS

# Schema.org property names whose mere presence in fields_present signals a
# real dataset structure (downloadable distribution, enumerated variables,
# linked catalog) even though the names themselves are filtered from the
# LLM rubric as page metadata. Subset of _PAGE_METADATA_FIELDS — they live
# in both lists by design: stripped from the rubric (so they don't trip
# schema-fit), but counted here as actionability evidence.
_DATASET_SIGNAL_FIELDS: frozenset[str] = frozenset({
    "distribution",
    "variablemeasured",
    "includedindatacatalog",
})


def _has_actionable_spec(source: DataSource) -> bool:
    """True when the source carries enough spec detail to actually serve as
    a CSV / API / file data source — not just a pointer page.

    For API: must have a real endpoint distinct from the homepage, OR a docs/
    OpenAPI/signup URL.
    For File: must have a download_url.
    For Embedded: must have at least one ``fields_present`` entry that isn't
    schema.org/OpenGraph page-level metadata (headline, name, description,
    breadcrumb, ...). A portal homepage whose only enumerated field is
    ``headline`` does NOT count as actionable. Catalog/service pages whose
    JSON-LD exposes ``availableChannel`` ServiceChannels with real endpoint
    URLs (REST API / search / sitemap) DO count — that channel IS the
    spec, even when the page surfaces no flat data columns. A page whose
    JSON-LD @type is itself a Dataset / DataDownload / Datafeed (or whose
    fields_present surfaces ``distribution`` / ``variableMeasured`` /
    ``includedInDataCatalog``) ALSO counts — those property names are
    stripped from the LLM rubric to avoid false schema-fit caps, but their
    presence is structural evidence of a real dataset.
    """
    if source.source_type == DataSourceType.API:
        spec = source.api_spec
        if not spec:
            return False
        if spec.documentation_url or spec.openapi_spec_url or spec.signup_url:
            return True
        # Endpoint counts as actionable only when it carries info beyond the
        # source homepage — otherwise it's just brand-page noise.
        if (
            spec.endpoint
            and spec.endpoint.rstrip("/") != (source.url or "").rstrip("/")
        ):
            return True
        return False
    if source.source_type == DataSourceType.DOWNLOADABLE_FILE:
        spec = source.file_spec
        if not spec or not spec.download_url:
            return False
        # Filter out CKAN/registry hits whose download_url actually points at
        # the dataset LANDING PAGE rather than a real resource. CKAN's
        # package_search returns a record-level entry with a `url` field that
        # is the human-facing dataset page; when no resource was lifted the
        # adapter falls back to that page as the download URL, the HEAD probe
        # returns text/html, and we end up with file_format='unknown' /
        # file_size=0 / content_type='text/html'. These are pointer pages,
        # not files — counting them as actionable downloads inflates the
        # registry's apparent recall (regression case: humdata
        # /dataset/2019-novel-coronavirus-cases scored 5.3/10 with
        # file_format=unknown, file_size=0, content_type='text/html').
        fmt = (spec.file_format or "").strip().lower()
        ct = (spec.content_type or "").strip().lower()
        size = spec.file_size or 0
        if fmt in ("", "unknown") and size == 0 and ct.startswith("text/html"):
            return False
        return True
    if source.source_type == DataSourceType.EMBEDDED_DATA:
        spec = source.embedded_spec
        if not spec:
            return False
        # Dataset-bearing JSON-LD @type — the page IS a dataset description.
        schema_norm = (spec.schema_type or "").strip().lower()
        if schema_norm in _DATASET_BEARING_SCHEMAS:
            return True
        # Normalize URI prefixes so RDFa-extracted fields like
        # ``https://ogp.me/ns#title`` and microdata ``http://schema.org/name``
        # collapse to bare ``title`` / ``name`` and match the lowercase
        # tokens in _DATASET_SIGNAL_FIELDS. Without this, a homepage scrape
        # whose only fields_present are ``https://ogp.me/ns#*`` Open Graph
        # URIs slipped past the page-metadata filter, looked actionable, and
        # surfaced as a useless data source (the iter 12 ``usgs.gov``
        # regression).
        fields_lower = {_normalize_field_name(f) for f in (spec.fields_present or [])}
        # Dataset-signal property names (distribution, variableMeasured, ...)
        # — stripped from the LLM rubric, but their presence proves the
        # JSON-LD describes a real dataset, not a brand page.
        if fields_lower & _DATASET_SIGNAL_FIELDS:
            return True
        # is_page_metadata_field handles both the bare-noun forms (already in
        # _PAGE_METADATA_FIELDS) AND the fully-qualified URI forms surfaced by
        # RDFa / JSON-LD extractors (https://ogp.me/ns#description, etc.)
        # which would otherwise slip through as "real fields" and falsely
        # mark a page as actionable (regression case: ECDC opendata RDFa-only
        # fields_present were counted as real columns, skipping format_fit
        # softening — though authority-based softening masked the bug, we
        # also use this same filter in the rubric below where it actively
        # changes the LLM's input).
        real_fields = [
            f for f in (spec.fields_present or [])
            if not is_page_metadata_field(f)
        ]
        if real_fields:
            return True
        return _embedded_has_service_channel(source)
    return False


def _sanitize_rationale(text: str) -> str:
    """Strip stray trailing quote characters left over from LLM JSON output.

    LLMs sometimes emit `"rationale": "...sentence.\""` — an escaped quote
    appended after the natural end of the sentence. After json.loads the field
    value carries that orphan quote (e.g. `'sentence."'`), which then leaks into
    the SSE / final report and looks like a serialization bug. Strip a single
    trailing quote when it follows ordinary terminal punctuation so we don't
    munge legitimately quoted phrases inside the rationale.
    """
    if not text:
        return text
    s = text.rstrip()
    if s.endswith('"') and len(s) >= 2 and s[-2] in '.!?;:':
        s = s[:-1].rstrip()
    return s


async def _score_single_source(
    source: DataSource, requirement: StructuredRequirement
) -> DataSource:
    """Compute all 5 dimension scores for a single source."""
    # Run relevance and authority in parallel (both may call LLM)
    relevance_task = _score_relevance(source, requirement)
    # Sources from llm_prior path were directly named by parse_intent in
    # known_authoritative_sources (or as a non-generic target_registries
    # entry — generic registries like huggingface/kaggle/openalex/ckan
    # are filtered upstream in `_llm_prior_sources`). Pass the flag so
    # score_authority can recognize them as vetted publishers instead of
    # falling back to the misleading "Unrecognized publisher (.gov +2.0
    # TLD bonus)" rationale for state-agency hosts that aren't in the
    # static domain prior table.
    authority_task = score_authority(
        source.url,
        source.metadata,
        source.provider,
        is_known_authoritative=(
            getattr(source, "discovery_method", "") == "llm_prior"
        ),
    )

    (relevance_score, relevance_rationale), (authority_score, authority_rationale) = (
        await asyncio.gather(relevance_task, authority_task)
    )
    logger.debug("Stage-B %s: relevance=%.1f (%s), authority=%.1f (%s)",
                  source.name[:40], relevance_score, relevance_rationale[:60],
                  authority_score, authority_rationale[:60])

    # Skip the deterministic relevance penalties when the LLM never delivered
    # a real score. Otherwise a Z.AI rate-limit storm — every source falls
    # back to relevance=5.0 with rationale `[scoring_failed] ...` — gets the
    # additional format_fit×0.7 multiplier applied, dropping 5.0 → 3.5 which
    # trips the 4.0 veto threshold and silently kills the entire candidate
    # set. The penalty is meant to override an LLM that overlooked schema
    # gaps, not to compound a missing-LLM fallback.
    skip_deterministic_relevance_penalty = _is_scoring_failed_rationale(relevance_rationale)

    # Hard temporal-constraint check: if temporal_coverage demonstrably fails
    # the requested range (e.g. source starts 2018 but user needs 2004-2024),
    # cap relevance so it cannot outrank sources that actually span the range.
    # The LLM rubric tends to reward topical match while overlooking a clear
    # date-range gap (see Databento 2018+ vs 20-year query case).
    if not skip_deterministic_relevance_penalty:
        temporal_multiplier = score_temporal_fit(
            source.temporal_coverage,
            requirement.temporal_range,
            name=source.name,
            description=source.description,
        )
        if temporal_multiplier < 1.0:
            original_relevance = relevance_score
            relevance_score = relevance_score * temporal_multiplier
            relevance_rationale = (
                f"[temporal_fit×{temporal_multiplier:.2f}: source coverage "
                f"{source.temporal_coverage!r} fails requested {requirement.temporal_range!r}] "
                + relevance_rationale
            )
            logger.debug(
                "Stage-B temporal penalty applied to %s: relevance %.1f → %.1f (mult=%.2f)",
                source.name[:40], original_relevance, relevance_score, temporal_multiplier,
            )

    # Stale embedded-page penalty: when an embedded source's RDFa-extracted
    # page metadata shows the page was last modified well before the
    # requested temporal range start, score_temporal_fit cannot catch it
    # because source.temporal_coverage often carries a broad portal-level
    # blurb ("COVID-19 from 2020") rather than the specific page's date.
    # Regression case: ECDC opendata RDFa modified_time=2020-12-14 (page is
    # literally titled "Download historical data (to 14 December 2020)") was
    # scored top for a 2022-2023 testing query.
    is_stale_page_archive = False
    if not skip_deterministic_relevance_penalty:
        stale_mult, stale_reason = _score_stale_page_fit(source, requirement)
        if stale_mult < 1.0:
            original_relevance = relevance_score
            relevance_score = relevance_score * stale_mult
            relevance_rationale = (
                f"[stale_page_fit×{stale_mult:.2f}: {stale_reason}] "
                + relevance_rationale
            )
            is_stale_page_archive = True
            logger.debug(
                "Stage-B stale-page penalty applied to %s: relevance %.1f → %.1f (mult=%.2f)",
                source.name[:40], original_relevance, relevance_score, stale_mult,
            )

    # Hard format-fit check: if the user explicitly asked for csv/api/json/etc.
    # but the source has no actionable spec (no real API endpoint, no
    # download URL, no enumerated data columns — i.e. it's a portal landing
    # page), demote relevance so a "pointer" page can't keep a 5.0 score
    # against the rubric's intent. The relevance LLM tends to reward topical
    # match (right domain, right geography) while overlooking that the page
    # cannot actually serve the requested CSV/API data — see EEA Air Quality
    # Portal homepage with fields_present=[headline] case.
    if not skip_deterministic_relevance_penalty:
        embedded_schema = (
            source.embedded_spec.schema_type
            if source.source_type == DataSourceType.EMBEDDED_DATA and source.embedded_spec
            else None
        )
        format_multiplier = score_format_fit(
            _has_actionable_spec(source),
            requirement.desired_formats,
            url=source.url,
            embedded_schema_type=embedded_schema,
        )
        # Soften the generic embedded-without-actionable-spec penalty (0.7) for
        # high-authority sources. An authoritative .gov/.edu/.org catalog
        # homepage IS the canonical entry point for the data — even when its
        # Schema.org JSON-LD strips down to catalog metadata only and
        # _has_actionable_spec can't see a Dataset @type or distribution
        # field. Without this softening, every catalog.data.gov /
        # waterqualitydata.us / *.epa.gov landing page gets demoted below the
        # 4.0 veto whenever the LLM assigns raw relevance < 5.72, silently
        # vetoing 9 of 10 stage-B candidates against EPA/USGS-flavoured
        # queries (regression case: "EPA Water Quality Portal nutrient and
        # dissolved oxygen" → 47 candidates → 1 surviving source). Keep the
        # 0.5 (docs URL) and 0.4 (Q&A schema) penalties active for those
        # cases — pure navigation/help pages aren't data sources regardless
        # of TLD.
        if format_multiplier == 0.7 and authority_score >= 7.0:
            logger.debug(
                "Stage-B format_fit×0.7 softened to 1.0 for high-authority %s "
                "(authority=%.1f): authoritative landing pages count as canonical",
                source.name[:40], authority_score,
            )
            format_multiplier = 1.0
        if format_multiplier < 1.0:
            original_relevance = relevance_score
            relevance_score = relevance_score * format_multiplier
            relevance_rationale = (
                f"[format_fit×{format_multiplier:.2f}: source type="
                f"{source.source_type.value} lacks actionable endpoint/download/data "
                f"fields, but user requested {requirement.desired_formats}] "
                + relevance_rationale
            )
            logger.debug(
                "Stage-B format penalty applied to %s: relevance %.1f → %.1f (mult=%.2f)",
                source.name[:40], original_relevance, relevance_score, format_multiplier,
            )

    # Geographic fit multiplier (2026-05-22): explicit deterministic check
    # for "user wants global, source covers one country" / "user wants
    # China, source covers US" — previously embedded in the LLM relevance
    # prompt where the LLM often missed it.
    if not skip_deterministic_relevance_penalty and settings.scoring.enable_geographic_fit:
        geo_multiplier = score_geographic_fit(
            source.geographic_coverage,
            requirement.geographic_scope,
        )
        if geo_multiplier < 1.0:
            original_relevance = relevance_score
            relevance_score = relevance_score * geo_multiplier
            relevance_rationale = (
                f"[geographic_fit×{geo_multiplier:.2f}: source coverage "
                f"{source.geographic_coverage or 'unspecified'} vs requested "
                f"{requirement.geographic_scope or 'unspecified'}] "
                + relevance_rationale
            )
            logger.debug(
                "Stage-B geographic penalty applied to %s: relevance %.1f → %.1f (mult=%.2f)",
                source.name[:40], original_relevance, relevance_score, geo_multiplier,
            )

    # Schema coverage multiplier (2026-05-22): replaces the LLM rubric's
    # SCHEMA-FIT GUARDRAIL with a deterministic ratio check. fields_present
    # comes from both embedded_spec and metadata (different writers stash
    # it in either location).
    if not skip_deterministic_relevance_penalty and settings.scoring.enable_schema_coverage_fit:
        fields_present: list[str] = []
        if source.embedded_spec and source.embedded_spec.fields_present:
            fields_present.extend(source.embedded_spec.fields_present)
        meta_fields = (source.metadata or {}).get("fields_present")
        if isinstance(meta_fields, list):
            fields_present.extend(meta_fields)

        schema_multiplier = score_schema_coverage_fit(
            fields_present,
            requirement.target_schema,
        )
        if schema_multiplier < 1.0:
            original_relevance = relevance_score
            relevance_score = relevance_score * schema_multiplier
            relevance_rationale = (
                f"[schema_coverage×{schema_multiplier:.2f}: source fields "
                f"don't sufficiently cover target schema] "
                + relevance_rationale
            )
            logger.debug(
                "Stage-B schema penalty applied to %s: relevance %.1f → %.1f (mult=%.2f)",
                source.name[:40], original_relevance, relevance_score, schema_multiplier,
            )


    # Deterministic scores
    # 2026-05-22: freshness can be disabled via ``enable_freshness_dim``.
    # When off, skip score_freshness() and store a 5.0 placeholder so
    # the SourceScores model still validates (field is ge=0/le=10).
    # is_retired is still read because aggregate() uses it for the
    # overall cap regardless of whether freshness contributes to the
    # weighted sum.
    is_retired = bool(source.metadata.get("is_retired", False))
    if settings.scoring.enable_freshness_dim:
        freshness_score = score_freshness(
            source.metadata.get("last_updated"),
            source.domain or requirement.domain,
        )
        if is_retired:
            # Retired/historical-only datasets get no forward-looking
            # freshness value — publisher said don't use for new work.
            freshness_score = 0.0
    else:
        freshness_score = 5.0  # neutral placeholder; weight=0 → ignored
    accessibility_score = score_accessibility(source.access_level)
    license_score = score_license_fit(
        source.license, requirement.license_constraint
    )

    # Aggregate with hard veto rules. llm_prior sources (named by
    # parse_intent in `known_authoritative_sources`) get a relaxed
    # relevance veto floor of 3.0 inside `aggregate` — see the docstring
    # there for the rationale. Without this, narrowly-worded queries
    # silently lose authoritative complementary sources (regression
    # iter 013: "USGS daily streamflow Utah" vetoed NOAA National Water
    # Model at relevance ~3.5 even though parse_intent had explicitly
    # named NOAA NWM as one of 5 authoritative sources, leaving the
    # final report with only 2 of 5 known sources surfaced).
    is_llm_prior = (
        getattr(source, "discovery_method", "") == "llm_prior"
    )
    # Disable the llm_prior salvage when the specific page that was
    # actually crawled is a news/content page rather than a dataset
    # description. A vetted catalog (data.europa.eu) crawled into a
    # news-article RDFa block (schema_type=schema.org/Article,
    # fields=[name, text]) cannot serve the user's structured data
    # query regardless of how authoritative the host domain is —
    # let the standard relevance<4 veto fire so a wrong-page crawl
    # can't pollute the final report via the publisher's salvage
    # exemption.
    if is_llm_prior and _embedded_is_content_page(source):
        logger.info(
            "Stage-B llm_prior salvage suppressed for %s: schema_type=%r is a "
            "content page (article/news/blog/webpage), not a dataset — "
            "falling back to standard veto floor",
            source.name[:40],
            (source.embedded_spec.schema_type if source.embedded_spec else None),
        )
        is_llm_prior = False
    # Disable the llm_prior salvage when the embedded page itself is a
    # verifiably stale archive (RDFa modified_time ≥1y before the requested
    # temporal range start). The publisher's catalog is authoritative for the
    # broader domain, but THIS specific page cannot serve the requested data
    # window — keeping it via salvage means the user's final report still
    # carries a stale archive masquerading as the canonical answer.
    # Regression case: opendata.ecdc.europa.eu RDFa modified_time=2020-12-14
    # was demoted by stale_page_fit×0.5 to relevance=3.0, but llm_prior salvage
    # (relaxed floor=3.0) kept it through the veto, surfacing a 2020 archive
    # in the final report for a 2023-2024 ECDC testing query.
    if is_llm_prior and is_stale_page_archive:
        logger.info(
            "Stage-B llm_prior salvage suppressed for %s: page is a "
            "verifiably stale archive (stale_page_fit fired) — "
            "falling back to standard veto floor",
            source.name[:40],
        )
        is_llm_prior = False
    overall = SourceScores.aggregate(
        relevance=relevance_score,
        authority=authority_score,
        freshness=freshness_score,
        accessibility=accessibility_score,
        license_fit=license_score,
        is_retired=is_retired,
        is_llm_prior=is_llm_prior,
    )
    if (
        is_llm_prior
        and overall > 0
        and relevance_score < settings.scoring.veto_relevance_threshold
    ):
        logger.info(
            "Stage-B llm_prior salvage: %s kept (relevance=%.1f below veto=%.1f, "
            "overall=%.2f) — parse_intent vetted as authoritative",
            source.name[:40], relevance_score,
            settings.scoring.veto_relevance_threshold, overall,
        )

    source.scores = SourceScores(
        relevance=relevance_score,
        authority=authority_score,
        freshness=freshness_score,
        accessibility=accessibility_score,
        license_fit=license_score,
        overall=overall,
        relevance_rationale=relevance_rationale,
        authority_rationale=authority_rationale,
    )
    logger.debug(
        "Stage-B final: %s → rel=%.1f auth=%.1f fresh=%.1f access=%.1f lic=%.1f → overall=%.1f",
        source.name[:40], relevance_score, authority_score, freshness_score,
        accessibility_score, license_score, overall,
    )

    return source


async def _score_single_source_v3(
    source: DataSource, requirement: StructuredRequirement
) -> DataSource:
    """v3 single-source scoring (2026-05-22).

    Differences from v2 (_score_single_source):
      1. Relevance is NOT computed here — Stage-A already set it.
      2. No relevance multipliers; format/temporal/geographic/schema fits
         are STANDALONE dims (0-10 instead of 0.4-1.0 multiplier scale).
      3. License veto goes through LLM confirmation when triggered.
      4. No weighted-sum overall — that's the LLM ranker's job.
      5. Veto still drops sources here (returns source with overall=-1).

    Tree pseudo-sources: many fields don't apply (no license, no spec).
    We fill placeholders that the LLM ranker recognizes as "N/A".
    """
    from src.judging.v3_helpers import is_tree_source

    # Stage-A already populated source.scores.relevance. Defensive init.
    if source.scores is None:
        logger.warning(
            "Stage-B v3: source %s has no Stage-A scores, defaulting to 5.0 relevance",
            source.name[:40],
        )
        source.scores = SourceScores(
            relevance=5.0, authority=5.0, freshness=5.0,
            accessibility=5.0, license_fit=5.0, overall=0.0,
        )
    is_tree = is_tree_source(source)

    # ── Authority (3-layer, no in-line LLM by default) ────────────────
    authority_score, authority_rationale = await score_authority(
        source.url,
        source.metadata or {},
        source.provider,
        is_known_authoritative=(
            getattr(source, "discovery_method", "") == "llm_prior"
        ),
    )

    # ── Accessibility (table lookup) ──────────────────────────────────
    accessibility_score = score_accessibility(source.access_level)

    # ── Freshness (may be disabled) ───────────────────────────────────
    is_retired = bool((source.metadata or {}).get("is_retired", False))
    if settings.scoring.enable_freshness_dim:
        freshness_score = score_freshness(
            (source.metadata or {}).get("last_updated"),
            source.domain or requirement.domain,
        )
        if is_retired:
            freshness_score = 0.0
    else:
        freshness_score = 5.0  # neutral placeholder

    # ── License fit (pure rule, no LLM) ────────────────────────────────
    # 2026-05-22: Reverted to pure rules. License compatibility is a
    # high-stakes legal task; the deterministic alias table (60+ entries
    # on both sides) covers practical edge cases. An LLM-confirmation
    # layer would introduce hallucination risk into the compliance path
    # with limited upside (rule misclassifications are rare after the
    # normalization work). When rule says veto, we veto.
    license_score = score_license_fit(
        source.license, requirement.license_constraint
    )
    license_rationale = ""
    if license_score == 0 and is_tree:
        # Trees don't carry license; the rule's veto can't fire correctly
        # on a missing field. Treat as neutral so the tree survives —
        # this is a deterministic exception for a structural reason, not
        # a license judgment.
        license_score = 5.0
        license_rationale = "tree_no_license_skipping_veto"

    # ── 4 fit-dim standalone scores (0-10) ─────────────────────────────
    embedded_schema = (
        source.embedded_spec.schema_type
        if source.source_type == DataSourceType.EMBEDDED_DATA and source.embedded_spec
        else None
    )
    format_fit_mult = score_format_fit(
        _has_actionable_spec(source) if not is_tree else True,  # trees treated as having spec
        requirement.desired_formats,
        url=source.url,
        embedded_schema_type=embedded_schema,
    )
    # Trees: no need for the 0.7-soften based on authority since trees
    # don't go through actionable_spec check the same way.
    if format_fit_mult == 0.7 and authority_score >= 7.0:
        format_fit_mult = 1.0
    format_fit_score = round(format_fit_mult * 10.0, 2)

    temporal_mult = score_temporal_fit(
        source.temporal_coverage,
        requirement.temporal_range,
        name=source.name,
        description=source.description,
    )
    temporal_fit_score = round(temporal_mult * 10.0, 2)

    geo_mult = score_geographic_fit(
        source.geographic_coverage,
        requirement.geographic_scope,
    )
    geographic_fit_score = round(geo_mult * 10.0, 2)

    fields_present: list[str] = []
    if source.embedded_spec and source.embedded_spec.fields_present:
        fields_present.extend(source.embedded_spec.fields_present)
    meta_fields = (source.metadata or {}).get("fields_present") or \
                  (source.metadata or {}).get("tree_fields_available")
    if isinstance(meta_fields, list):
        fields_present.extend(meta_fields)
    schema_mult = score_schema_coverage_fit(
        fields_present,
        requirement.target_schema,
    )
    schema_coverage_score = round(schema_mult * 10.0, 2)

    # ── Veto check (license=0 + LLM confirmed, or relevance too low) ──
    is_llm_prior = (getattr(source, "discovery_method", "") == "llm_prior")
    relevance_score = source.scores.relevance   # from Stage-A
    veto_overall = -1

    def _emit_dim_scored(decision: str, vetoed_by: str | None, veto_reason: str = "") -> None:
        log_event("stage_b.dim_scored", {
            "id": source.id,
            "name": source.name[:80],
            "url": source.url[:120],
            "source_kind": "tree" if is_tree else "flat",
            "discovery_method": getattr(source, "discovery_method", "") or "",
            "is_llm_prior": is_llm_prior,
            "is_retired": is_retired,
            "dims": {
                "relevance": round(relevance_score, 2),
                "authority": round(authority_score, 2),
                "freshness": round(freshness_score, 2),
                "accessibility": round(accessibility_score, 2),
                "license_fit": round(license_score, 2),
                "format_fit": round(format_fit_score, 2),
                "temporal_fit": round(temporal_fit_score, 2),
                "geographic_fit": round(geographic_fit_score, 2),
                "schema_coverage": round(schema_coverage_score, 2),
            },
            "decision": decision,
            "vetoed_by": vetoed_by,
            "veto_reason": veto_reason[:200],
            "authority_rationale": (authority_rationale or "")[:200],
            "license_rationale": (license_rationale or "")[:200],
        })

    # License veto (LLM confirmed)
    if license_score == 0 and settings.scoring.veto_license_zero:
        source.scores.relevance = relevance_score
        source.scores.authority = authority_score
        source.scores.freshness = freshness_score
        source.scores.accessibility = accessibility_score
        source.scores.license_fit = license_score
        source.scores.format_fit = format_fit_score
        source.scores.temporal_fit = temporal_fit_score
        source.scores.geographic_fit = geographic_fit_score
        source.scores.schema_coverage_fit = schema_coverage_score
        source.scores.overall = veto_overall
        source.scores.authority_rationale = authority_rationale
        source.scores.license_rationale = license_rationale
        logger.info("Stage-B v3 vetoed (license): %s — %s",
                    source.name[:40], license_rationale)
        _emit_dim_scored("vetoed", "license", license_rationale or "license_fit=0")
        return source
    # Relevance veto (Stage-A scored too low)
    veto_floor = 3.0 if is_llm_prior else settings.scoring.veto_relevance_threshold
    if relevance_score < veto_floor:
        source.scores.relevance = relevance_score
        source.scores.authority = authority_score
        source.scores.freshness = freshness_score
        source.scores.accessibility = accessibility_score
        source.scores.license_fit = license_score
        source.scores.format_fit = format_fit_score
        source.scores.temporal_fit = temporal_fit_score
        source.scores.geographic_fit = geographic_fit_score
        source.scores.schema_coverage_fit = schema_coverage_score
        source.scores.overall = veto_overall
        source.scores.authority_rationale = authority_rationale
        source.scores.license_rationale = license_rationale
        logger.info(
            "Stage-B v3 vetoed (relevance %.1f < %.1f): %s",
            relevance_score, veto_floor, source.name[:40],
        )
        _emit_dim_scored(
            "vetoed", "relevance",
            f"relevance={relevance_score:.2f} below floor={veto_floor:.2f}",
        )
        return source

    # ── Survived: populate all dims; overall=0 placeholder for LLM ranker ──
    source.scores.relevance = relevance_score
    source.scores.authority = authority_score
    source.scores.freshness = freshness_score
    source.scores.accessibility = accessibility_score
    source.scores.license_fit = license_score
    source.scores.format_fit = format_fit_score
    source.scores.temporal_fit = temporal_fit_score
    source.scores.geographic_fit = geographic_fit_score
    source.scores.schema_coverage_fit = schema_coverage_score
    # If is_retired, even after LLM ranker overall gets capped at 4.0
    source.scores.overall = 0.0  # placeholder — LLM ranker sets it
    source.scores.authority_rationale = authority_rationale
    if license_rationale:
        source.scores.license_rationale = license_rationale

    logger.debug(
        "Stage-B v3: %s → rel=%.1f auth=%.1f fresh=%.1f access=%.1f lic=%.1f "
        "fmt=%.1f temp=%.1f geo=%.1f schema=%.1f",
        source.name[:40], relevance_score, authority_score, freshness_score,
        accessibility_score, license_score, format_fit_score,
        temporal_fit_score, geographic_fit_score, schema_coverage_score,
    )
    _emit_dim_scored("kept", None)
    return source


async def stage_b_score(
    sources: list[DataSource],
    requirement: StructuredRequirement,
    out_timings: dict[str, float] | None = None,
) -> list[DataSource]:
    """Stage-B scoring.

    v3 path (default): per-dim scoring (no relevance LLM since Stage-A
    already did it) + LLM-confirmed license veto + LLM ranker.

    v2 path (legacy): full 5-dim scoring with relevance LLM + 5
    multipliers + weighted sum aggregate. Activated by setting
    ``enable_stage_a_scoring=False`` so Stage-A doesn't pre-populate
    relevance — Stage-B falls back to computing it itself.

    ``out_timings`` (optional): when provided, sub-stage durations are
    written into this dict with keys ``stage_b.dim_scoring`` /
    ``stage_b.ranker`` / ``stage_b.total``.
    """
    if not sources:
        return []

    timings = out_timings if out_timings is not None else {}
    overall_start = time.monotonic()

    use_v3 = settings.scoring.enable_stage_a_scoring
    scorer = _score_single_source_v3 if use_v3 else _score_single_source

    # ── Per-source dim scoring (concurrent) ────────────────────────────
    t0 = time.monotonic()
    scored = await asyncio.gather(
        *[scorer(s, requirement) for s in sources],
        return_exceptions=True,
    )
    timings["stage_b.dim_scoring"] = time.monotonic() - t0

    results: list[DataSource] = []
    for result in scored:
        if isinstance(result, DataSource):
            results.append(result)
        else:
            logger.warning("Stage-B scoring error: %s", result)

    logger.info(
        "Stage-B scored %d/%d sources (mode=%s, dim_scoring=%.2fs)",
        len(results), len(sources), "v3" if use_v3 else "v2",
        timings["stage_b.dim_scoring"],
    )

    # ── v3: LLM ranker as final step ──────────────────────────────────
    if use_v3 and settings.scoring.enable_stage_b_llm_ranker:
        # Pass surviving sources (not vetoed) to ranker; vetoed sources
        # have overall=-1 and judge_node filters them out anyway.
        survivors = [s for s in results if s.scores and s.scores.overall != -1]
        vetoed = [s for s in results if s not in survivors]

        t0 = time.monotonic()
        from src.judging.v3_helpers import llm_rank_sources
        ranked_survivors = await llm_rank_sources(
            survivors, requirement.original_query
        )
        timings["stage_b.ranker"] = time.monotonic() - t0
        logger.info(
            "Stage-B ranker: %.2fs over %d survivors",
            timings["stage_b.ranker"], len(survivors),
        )
        # Apply retired cap AFTER ranker
        for s in ranked_survivors:
            if s.scores and (s.metadata or {}).get("is_retired", False):
                s.scores.overall = min(s.scores.overall, settings.scoring.retired_overall_cap)
        # Return ranked survivors followed by vetoed (judge_node filters
        # overall>0 anyway, so vetoed dropped there).
        timings["stage_b.total"] = time.monotonic() - overall_start
        return ranked_survivors + vetoed

    timings["stage_b.total"] = time.monotonic() - overall_start
    return results
