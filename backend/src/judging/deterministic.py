"""Deterministic scoring for Freshness, Accessibility, and License Fit.

These three dimensions are computed purely by rules — never by LLM.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

from src.config import settings
from src.models.data_source import AccessLevel
from src.models.scores import get_accessibility_scores, get_freshness_half_life


def score_freshness(last_updated: datetime | str | None, domain: str = "default") -> float:
    """Score freshness using exponential decay based on domain-specific half-life.

    Returns 0-10 score.
    """
    if last_updated is None:
        return settings.scoring.freshness_unknown_score  # Unknown → neutral score

    if isinstance(last_updated, str):
        try:
            last_updated = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return settings.scoring.freshness_unknown_score

    days_old = max(0, (datetime.utcnow() - last_updated.replace(tzinfo=None)).days)
    freshness_half_life = get_freshness_half_life()
    half_life = freshness_half_life.get(domain, freshness_half_life["default"])
    # Guard a misconfigured 0/negative half-life (these are config-tunable):
    # without it the division below raises ZeroDivisionError and the whole
    # source is silently dropped by stage_b's gather(return_exceptions=True).
    half_life = max(1, half_life)

    # Exponential decay: score = 10 * 0.5^(days/half_life)
    score = 10.0 * math.pow(0.5, days_old / half_life)
    return round(max(0, min(10, score)), 2)


def score_accessibility(access_level: AccessLevel | str) -> float:
    """Score accessibility by direct lookup table.

    OPEN=10, FREE_REG=8, API_KEY_FREE=7, OAUTH=6, API_KEY_PAID=4, PAYWALL=2, UNKNOWN=3.
    """
    if isinstance(access_level, AccessLevel):
        key = access_level.value
    else:
        key = str(access_level).lower()

    accessibility_scores = get_accessibility_scores()
    return accessibility_scores.get(key, accessibility_scores.get("unknown", 3.0))


# ──────────────────────────────────────────────────────────────────────
# License normalization (added 2026-05-22)
#
# Agents and adapters write license strings in 17+ distinct flavors based
# on empirical data — "open-source" / "government-open-data" / "CC BY 4.0"
# (with spaces) / "署名-相同方式共享 4.0 国际" / etc. The downstream
# substring matcher in score_license_fit() only recognizes the SPDX-flavored
# slugs (cc-by, mit, apache-2.0, ...). Without normalization, ~80% of
# real-world license strings fall through to the default 6.0 even when
# they clearly express open / NC / proprietary intent.
#
# This normalizer maps free-form strings to canonical tokens that the
# existing substring matcher recognizes. Strict alias table + a couple
# of pattern-based rules. No LLM at scoring time (would be too slow); a
# config flag allows a future offline-cache LLM normalizer.
# ──────────────────────────────────────────────────────────────────────


_LICENSE_ALIAS_TABLE: dict[str, str] = {
    # === Open / CC family ===
    "open": "open government",  # bare "open" → recognized as OPEN
    "open-source": "open government",
    "opensource": "open government",
    "open source": "open government",
    "open-data": "open government",
    "open data": "open government",
    "open government data": "open government",
    "government open data": "open government",
    "government-open-data": "open government",
    "gov-open-data": "open government",
    "ogl": "open government",
    "open government license": "open government",
    # CC variants (LLM/agent writes both space and dash separated)
    "cc by 4.0": "cc-by-4.0",
    "cc by-4.0": "cc-by-4.0",
    "cc-by 4.0": "cc-by-4.0",
    "cc by 3.0": "cc-by-3.0",
    "cc by-sa 4.0": "cc-by-sa-4.0",
    "cc-by-sa 4.0": "cc-by-sa-4.0",
    "cc by-sa": "cc-by-sa",
    "creative commons attribution": "cc-by",
    "creative commons": "cc-by",
    "cc zero": "cc0",
    "cc-zero": "cc0",
    "public-domain": "public domain",
    # Permissive
    "mit license": "mit",
    "the mit license": "mit",
    "mit-style": "mit",
    "apache license 2.0": "apache-2.0",
    "apache 2.0": "apache-2.0",
    "apache license": "apache-2.0",
    "bsd-3-clause": "bsd",
    "bsd 3-clause": "bsd",
    "bsd-2-clause": "bsd",
    "bsd license": "bsd",
    # ODC / OSM
    "odbl": "odc-odbl",
    "open database license": "odc-odbl",
    "open data commons": "odc-odbl",
    "odc-by": "odc-by",
    # Copyleft
    "gnu gpl": "gpl",
    "gnu general public license": "gpl",
    "gpl v3": "gpl-3.0",
    "gpl-v3": "gpl-3.0",
    "gpl-3": "gpl-3.0",
    "gpl v2": "gpl-2.0",
    "agpl v3": "agpl-3.0",
    "lgpl v3": "lgpl-3.0",
    # === Non-commercial / restricted ===
    "nc": "cc-by-nc",
    "non commercial": "non-commercial",
    "non-commercial-only": "non-commercial",
    "academic": "academic use only",
    "academic only": "academic use only",
    "academic-only": "academic use only",
    "research": "research only",
    "research-only": "research only",
    "personal": "personal use only",
    "personal-only": "personal use only",
    # === Proprietary / commercial ===
    "commercial": "commercial license",  # bare "commercial" → recognized as PROPRIETARY
    "commercial-only": "commercial license",
    "paid-only": "paid",
    "subscription": "paid",
    "subscription-only": "paid",
    "news-media": "proprietary",       # news/media content is copyrighted
    "news media": "proprietary",
    "media": "proprietary",
    # === 中文别名 ===
    "署名": "cc-by",
    "署名-相同方式共享": "cc-by-sa",
    "署名-非商业性使用": "cc-by-nc",
    "署名-禁止演绎": "cc-by-nd",
    "署名-相同方式共享 4.0 国际": "cc-by-sa-4.0",
    "署名 4.0 国际": "cc-by-4.0",
    "公有领域": "public domain",
    "开源": "open government",
    "开放": "open government",
    "开放数据": "open government",
    "政府开放数据": "open government",
    "商用": "commercial license",
    "商业": "commercial license",
    "商业授权": "commercial license",
    "非商用": "cc-by-nc",
    "非商业": "cc-by-nc",
    "仅限学术": "academic use only",
    "学术使用": "academic use only",
    # === Pass-throughs (already canonical or close enough) ===
    "unknown": "",     # treat as None
    "varies": "",      # treat as None
    "n/a": "",
    "na": "",
}


# ──────────────────────────────────────────────────────────────────────
# Constraint (user-intent) side normalization (added 2026-05-22)
#
# parse_intent's LLM emits ``requirement.license_constraint`` as a
# free-form string; the spec calls for one of {any, commercial, open_only}
# but in practice the LLM writes variants like "commercial use",
# "open source", "freemium", "商用", "open_only or freemium", etc.
# Without normalization, score_license_fit's exact-match branches
# (`== "commercial"` / `== "open_only"`) silently miss them, dropping
# the entire constraint signal back to "any" or the default 6.0 fallback.
#
# This table maps known constraint variants (Chinese + English) to the
# 3 canonical values. Used both by score_license_fit() at runtime and
# by the StructuredRequirement validator at model-boundary time.
# ──────────────────────────────────────────────────────────────────────

_CONSTRAINT_CANONICAL = ("any", "commercial", "open_only")

_CONSTRAINT_ALIAS_TABLE: dict[str, str] = {
    # === Canonical (identity) ===
    "any": "any",
    "commercial": "commercial",
    "open_only": "open_only",

    # === English ANY-equivalents ===
    "anything": "any",
    "no constraint": "any",
    "no-constraint": "any",
    "unconstrained": "any",
    "no preference": "any",
    "doesn't matter": "any",
    "doesnt matter": "any",
    "n/a": "any",
    "na": "any",
    "none": "any",
    "null": "any",
    "unknown": "any",

    # === English COMMERCIAL-equivalents ===
    "commercial use": "commercial",
    "commercial-use": "commercial",
    "commercial_use": "commercial",
    "commercial only": "commercial",
    "commercial-only": "commercial",
    "for commercial use": "commercial",
    "for-profit": "commercial",
    "for profit": "commercial",
    "for_profit": "commercial",
    "profit": "commercial",
    "business": "commercial",
    "business use": "commercial",
    "for-business": "commercial",
    "enterprise": "commercial",
    "production": "commercial",
    "production use": "commercial",
    "paid": "commercial",  # user willing to pay → commercial intent
    "paid use": "commercial",

    # === English OPEN_ONLY-equivalents ===
    "open": "open_only",
    "open-only": "open_only",
    "openonly": "open_only",
    "open source": "open_only",
    "open-source": "open_only",
    "opensource": "open_only",
    "free": "open_only",       # "free as in libre" — best guess
    "free-only": "open_only",
    "free only": "open_only",
    "free use": "open_only",
    "no cost": "open_only",
    "no-cost": "open_only",
    "fully open": "open_only",
    "fully-open": "open_only",
    "permissive": "open_only",
    "libre": "open_only",
    "must be free": "open_only",
    "must be open": "open_only",

    # === Edge: user's use IS non-commercial/research → "any" (NC OK for them) ===
    # These describe the user's USE pattern, not a license demand. They
    # accept NC licenses (which "open_only" would penalize via 4.0 cap),
    # so map to "any" to avoid unfairly penalizing NC sources.
    "non-commercial": "any",
    "noncommercial": "any",
    "non commercial": "any",
    "nc": "any",
    "personal": "any",
    "personal use": "any",
    "personal-use": "any",
    "individual": "any",
    "individual use": "any",
    "research": "any",
    "research use": "any",
    "research-use": "any",
    "academic": "any",
    "academic use": "any",
    "academic-use": "any",
    "non-profit": "any",
    "nonprofit": "any",
    "non profit": "any",
    "education": "any",
    "educational": "any",
    "freemium": "any",  # freemium is a BUDGET concern, not license

    # === 中文 ===
    "任意": "any",
    "都行": "any",
    "随意": "any",
    "无要求": "any",
    "无限制": "any",
    "不限": "any",
    "未指定": "any",

    "商用": "commercial",
    "商业": "commercial",
    "商业用途": "commercial",
    "商业使用": "commercial",
    "营利": "commercial",
    "营利性": "commercial",
    "盈利": "commercial",
    "企业使用": "commercial",
    "付费": "commercial",
    "可付费": "commercial",

    "开源": "open_only",
    "开放": "open_only",
    "开源协议": "open_only",
    "公开": "open_only",
    "公开数据": "open_only",
    "免费开源": "open_only",
    "免费且开源": "open_only",

    "非商用": "any",
    "非商业": "any",
    "非商业用途": "any",
    "个人使用": "any",
    "个人用途": "any",
    "学术": "any",
    "学术使用": "any",
    "学术用途": "any",
    "研究": "any",
    "研究使用": "any",
    "科研": "any",
    "非营利": "any",
    "公益": "any",
    "教学": "any",
    "教育": "any",
    "免费": "any",  # 免费 → about cost, not license
}


def normalize_license_constraint(value: str | None) -> str:
    """Map a free-form user-constraint string to one of the canonical
    three values: ``any`` / ``commercial`` / ``open_only``.

    Strategy:
      1. None / empty / whitespace → "any" (safe default)
      2. Lowercased + collapsed-whitespace exact match against alias table
      3. ``"any" / "any ..."`` prefix → "any"  (existing parse_intent variants
         like "any (preferably open or research-friendly)")
      4. Substring containment heuristic:
           - if any commercial keyword in input → "commercial"
           - elif any open keyword in input → "open_only"
           - elif any "research/academic/personal" keyword → "any"
      5. Fallback: "any" (safe — neutral default never causes wrong veto)
    """
    if value is None:
        return "any"
    raw = str(value).strip().lower()
    if not raw:
        return "any"
    raw = re.sub(r"\s+", " ", raw)

    # 1. Exact alias hit
    canonical = _CONSTRAINT_ALIAS_TABLE.get(raw)
    if canonical:
        return canonical

    # 2. "any ..." prefix (e.g., "any (preferably open)" — historical behavior)
    if raw == "any" or raw.startswith("any "):
        return "any"

    # 3. Substring heuristic for unrecognized compound phrases like
    #    "open_only or freemium" / "commercial preferred, otherwise any"
    commercial_signals = (
        "commercial", "for profit", "for-profit", "business",
        "enterprise", "production", "商用", "商业", "营利", "盈利",
    )
    open_signals = (
        "open source", "open-source", "opensource", "open_only", "open-only",
        "free", "libre", "公开", "开源", "开放",
    )
    research_signals = (
        "non-commercial", "noncommercial", "non commercial", "research",
        "academic", "personal", "individual", "non-profit",
        "非商用", "非商业", "学术", "研究", "个人", "公益",
    )

    if any(sig in raw for sig in commercial_signals):
        return "commercial"
    if any(sig in raw for sig in open_signals):
        return "open_only"
    if any(sig in raw for sig in research_signals):
        return "any"

    # 4. Fallback to safe default
    return "any"


def _normalize_license(source_license: str | None) -> str | None:
    """Map a free-form license string to a canonical form recognized by
    the substring matchers in ``score_license_fit``.

    Strategy:
      1. Lower-case + collapse whitespace.
      2. Exact-match the alias table.
      3. If no alias hits, return the lower-cased input unchanged (lets
         the existing substring matcher try — preserves backward compat).
      4. Special-case: empty / placeholder values ("unknown" / "n/a" / "")
         map to None so caller treats it as "no license info".

    Examples (tested):
      "CC BY 4.0"                  → "cc-by-4.0"
      "open-source"                → "open government"
      "ODbL"                       → "odc-odbl"
      "署名-相同方式共享 4.0 国际"  → "cc-by-sa-4.0"
      "unknown"                    → None
      "Apache License 2.0"         → "apache-2.0"
      "MIT"                        → "mit" (lower-cased, alias passthrough)
    """
    if source_license is None:
        return None
    raw = str(source_license).strip().lower()
    if not raw:
        return None
    # Collapse internal whitespace to single space so "cc  by  4.0" → "cc by 4.0".
    raw = re.sub(r"\s+", " ", raw)
    alias = _LICENSE_ALIAS_TABLE.get(raw)
    if alias is not None:
        return alias or None  # "" alias means treat as unknown
    return raw


def score_license_fit(
    source_license: str | None,
    user_constraint: str = "any",
) -> float:
    """Score license fit using SPDX-based rule matching.

    Returns 0 (hard veto) if constraint=commercial and license is non-commercial.
    NEVER let LLM judge license compatibility — use hard rules. (2026-05-22:
    Added ``_normalize_license`` in front to handle the 17+ chaotic strings
    agents/adapters write in practice; the rule-based matcher itself is
    unchanged downstream of normalization.)
    """
    # 2026-05-22: Constraint-side now goes through a full alias-table
    # normalizer (Chinese + English variants) so parse_intent's free-form
    # output like "commercial use" / "open source" / "商用" / "研究使用"
    # all resolve to one of the 3 canonical values before hitting the
    # branch logic. Symmetric with the source-side _normalize_license.
    constraint_norm = normalize_license_constraint(user_constraint)
    if constraint_norm == "any":
        return 8.0  # No constraint → good score

    # Normalize the source license string before substring matching.
    license_lower = _normalize_license(source_license)
    if license_lower is None:
        if constraint_norm == "commercial":
            return 3.0  # Unknown license with commercial need → risky
        return 5.0  # Unknown → neutral

    # === Non-commercial licenses ===
    # 2026-05-22: NC detection used to only fire when constraint=="commercial".
    # That left an open_only user with a CC-BY-NC source scoring 10.0 (because
    # "cc-by" substring-matched OPEN before NC was checked). Now we detect NC
    # FIRST regardless of constraint; commercial gets the 0.0 veto, open_only
    # gets a softer penalty, and other constraints fall through to the OPEN
    # check only when the source isn't NC.
    NON_COMMERCIAL = [
        "cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd",
        "non-commercial", "noncommercial",
        "academic use only", "research only",
        "personal use only",
    ]
    is_nc = any(nc in license_lower for nc in NON_COMMERCIAL)

    if is_nc:
        if constraint_norm == "commercial":
            return 0.0  # HARD VETO — license doesn't permit commercial use
        if constraint_norm == "open_only":
            return 4.0  # NC has restrictions → can't satisfy "fully open" intent
        return 7.0  # Allowed for general/research use

    # === Open licenses ===
    OPEN_LICENSES = [
        "mit", "apache-2.0", "bsd", "isc", "unlicense",
        "cc0", "cc-by", "cc-by-sa",
        "public domain", "pdm", "odc-by", "odc-odbl",
        "open government", "ogdl",
    ]

    if any(ol in license_lower for ol in OPEN_LICENSES):
        return 10.0

    # === Free-with-attribution (CC-BY-equivalent in practice) ===
    if "free" in license_lower and ("citation" in license_lower or "attribution" in license_lower):
        return 8.0

    # === Copyleft (usable but with conditions) ===
    COPYLEFT = ["gpl", "agpl", "lgpl", "cc-by-sa"]
    if any(cl in license_lower for cl in COPYLEFT):
        return 7.0 if constraint_norm != "commercial" else 5.0

    # === Paid / proprietary ===
    PROPRIETARY = ["proprietary", "commercial license", "paid"]
    if any(p in license_lower for p in PROPRIETARY):
        if constraint_norm == "open_only":
            return 0.0  # Veto: user wants open, source is proprietary
        return 6.0  # OK if user is fine paying

    return 6.0  # Default for unrecognized licenses


def _extract_year_range(text: str | None) -> tuple[int | None, int | None]:
    """Extract (min_year, max_year) from a free-form date string.

    Handles common formats found in our pipeline:
      - "2004-2024"                  → (2004, 2024)
      - "2018-05-01/.."              → (2018, <current year>)  (open-ended: runs to today)
      - "2020-01-01/2023-12-31"      → (2020, 2023)
      - "2004-2024 (minimum 20 yrs)" → (2004, 2024)
      - "latest", "any", None        → (None, None)
    """
    if not text:
        return (None, None)
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
    if not years:
        return (None, None)
    lo, hi = min(years), max(years)
    # ISO 8601 open-ended interval ("2018-05-01/..") or "…–present"/"ongoing"
    # means the range runs through today. Expand the upper bound to the current
    # year so the span reflects the real multi-year request — otherwise a lone
    # start year collapses the span to 0 and score_temporal_fit's latest-only
    # demotion (req_span >= 2) never fires for open-ended requests.
    if re.search(r"/\s*\.\.|\.\.\s*$|\bpresent\b|\bongoing\b", text.lower()):
        hi = max(hi, datetime.utcnow().year)
    return (lo, hi)


# URL path segments that mark a page as documentation/help/about — not the
# data itself. Matched as path segments so we don't mistake e.g.
# `data.gov/dataset/about-air-quality` (where the dataset *is* about a
# documentation topic) for a docs page; segment match keeps the signal narrow.
_DOC_PATH_SEGMENTS = (
    "/docs/", "/docs",
    "/help/", "/help",
    "/faq/", "/faq",
    "/about/", "/about",
    "/terms/", "/terms",
    "/support/", "/support",
    "/documentation/", "/documentation",
    "/manual/", "/manual",
    "/guide/", "/guides/",
    "/tutorial/", "/tutorials/",
    "/reference/",  # language/API reference pages, e.g. /reference/readNWIS
)


def _is_documentation_url(url: str | None) -> bool:
    """True when the URL path looks like a documentation/help/about page.

    Pure URL pattern check — not metadata-based — because documentation pages
    consistently live under predictable paths but rarely carry self-identifying
    metadata that the LLM relevance rubric can latch onto.
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        path = (urlparse(url).path or "").rstrip("/").lower()
    except Exception:
        return False
    if not path:
        return False
    # Match only when a marker segment is at the END of the path or is a full
    # segment — protects e.g. ``/data/about-stations.csv`` (data file whose
    # filename happens to contain "about") from being demoted.
    for seg in _DOC_PATH_SEGMENTS:
        s = seg.rstrip("/")
        if path.endswith(s) or (s + "/") in (path + "/"):
            return True
    return False


# Schema.org types whose payload is conversational Q&A, step-by-step help,
# or biographical/entity descriptions rather than dataset records. A page
# emitting these as its only JSON-LD/microdata type is *describing an
# entity or answering questions*, not serving the data.
#
# Iter 11 regression: Investing.com /etfs/.../historical-data extracted only a
# FAQPage with 3 generic "today's price" Q&A entries, scored relevance 7.0 by
# the LLM (URL contained "historical-data"), survived format_fit×0.7 → 4.9,
# and made it into the final report as a "historical data" source.
#
# Iter 010 addition (Person/Organization/...): water.ca.gov homepage exposed
# only schema.org/Person microdata for executives (Gavin Newsom, Wade
# Crowfoot, Karla Nemeth) for a "California water reservoir storage / water
# rights" query. The LLM scored relevance 7.0 from URL+description match
# without seeing the extracted payload, format_fit×0.7 was softened to 1.0
# by the high-authority rule (water.ca.gov authority=9), and the source
# surfaced with overall=6.3 — semantically irrelevant Person bios masquerading
# as a water-data source. Person/Organization/etc. describe an entity, not a
# dataset; treat them like Q&A schemas (0.4 multiplier).
_NON_DATA_EMBEDDED_SCHEMAS: frozenset[str] = frozenset({
    "faqpage",
    "qapage",
    "question",
    "howto",
    "howtostep",
    "howtosection",
    "howtotip",
    "claim",
    "claimreview",
    # Biographical / entity-description schemas — these JSON-LD/microdata
    # blocks describe a person, organization, or business, not a dataset.
    # Common on agency homepages (executive bios) and corporate "about"
    # sections.
    "person",
    "organization",
    "corporation",
    "ngo",
    "governmentorganization",
    "educationalorganization",
    "localbusiness",
    "place",
    "postaladdress",
})


def _normalize_schema_type(schema_type: str | None) -> str:
    """Strip URI prefix / fragment from a Schema.org @type so set lookups work.

    Microdata extraction emits full URIs (e.g. ``http://schema.org/Person``)
    while JSON-LD typically uses bare type names (e.g. ``Person``). Without
    normalization, frozenset lookups against bare lowercase tokens silently
    fail for microdata-extracted sources — which is exactly how Person bio
    microdata on water.ca.gov dodged the format_fit penalty.
    """
    if not schema_type:
        return ""
    s = schema_type.strip().lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if "#" in s:
        s = s.rsplit("#", 1)[-1]
    return s


def score_format_fit(
    has_actionable_spec: bool,
    desired_formats: list[str] | None,
    url: str | None = None,
    embedded_schema_type: str | None = None,
) -> float:
    """Return a relevance multiplier in [0.4, 1.0] based on format-fit.

    When the user explicitly asks for structured/machine-readable data (csv,
    json, api, parquet, xml, xlsx, tsv) and the discovered source has no
    actionable spec — no API endpoint distinct from the homepage, no file
    download URL, no enumerated data columns — the source is at best a
    *pointer* to where the data lives, not the data itself. Demote so a
    portal homepage with empty data_format and only schema.org page-metadata
    fields cannot keep the same relevance as a real dataset that satisfies
    the format requirement.

    Documentation/help/about pages get a stronger demotion (0.5) — even when
    they technically carry an api_spec endpoint that equals the page URL,
    they're navigation landing pages, not data. Without this, ``/docs`` URLs
    routinely score ~5.0 and outrank actual data endpoints (regression case:
    USGS NWIS ``/docs`` page reaching 4.9 against a 4.0 veto threshold).

    Embedded sources whose only JSON-LD type is FAQPage/QAPage/HowTo get the
    strongest demotion (0.4) — that JSON-LD describes Q&A or step-by-step
    answers about the page topic, not data records. A FAQPage with three
    generic "today's price" questions is not a historical-data source no
    matter how much the URL says "historical-data".

    Pairs with the existing ``score_temporal_fit`` post-LLM penalty: the
    relevance LLM tends to reward topical/domain match while overlooking that
    a portal landing page (source_type=embedded, fields_present=[headline])
    cannot actually serve CSV/API data.

    Returns:
      1.0  when no format constraint, OR the user accepts unstructured output
           (e.g., desired_formats has only "any"), OR the source carries an
           actionable api/file/data-fields spec AND is not a documentation
           page.
      0.7  when desired_formats demands structured data and the source has no
           actionable spec (e.g., embedded portal homepage).
      0.5  when the URL is a documentation/help/about/reference page —
           applied regardless of has_actionable_spec, because such pages are
           navigation, not data.
      0.4  when the embedded schema_type is a Q&A/help schema
           (FAQPage / QAPage / HowTo / ...) — these pages explicitly host
           conversational answers, not dataset rows.
    """
    if not desired_formats:
        return 1.0
    structured = {"csv", "json", "xml", "parquet", "xlsx", "tsv", "api"}
    wants_structured = any(
        str(f).lower().strip() in structured for f in desired_formats
    )
    if not wants_structured:
        return 1.0
    if embedded_schema_type and _normalize_schema_type(embedded_schema_type) in _NON_DATA_EMBEDDED_SCHEMAS:
        return 0.4
    if _is_documentation_url(url):
        return 0.5
    if has_actionable_spec:
        return 1.0
    return 0.7


# "Latest-only"/"current-only"/"realtime" markers in source name/description
# that indicate a feed serves *only* the most-recent observation, not the
# historical archive — incompatible with multi-year historical queries.
_LATEST_ONLY_RE = re.compile(
    r"\b(latest|current|most[\s\-]?recent|real[\s\-]?time|realtime|"
    r"instantaneous values?|today's|now|live|nowcast)\b",
    re.IGNORECASE,
)


def score_temporal_fit(
    temporal_coverage: str | None,
    requested_range: str | None,
    name: str | None = None,
    description: str | None = None,
) -> float:
    """Return a relevance multiplier in [0.4, 1.0] based on temporal-coverage fit.

    The user's requirement may carry a hard temporal constraint (e.g. "20 years
    of history" → 2004-2024). When a source's `temporal_coverage` clearly fails
    to span that requested range, the LLM relevance score alone is a poor signal
    — it tends to reward topical match while ignoring the date-range gap. This
    deterministic post-check pulls such sources down so they don't outrank ones
    that actually satisfy the constraint.

    When ``temporal_coverage`` is missing (common: enrichment fails to populate
    it), fall back to a name/description scan for "latest"/"current"/"realtime"
    markers — endpoints labeled ``Latest daily values`` or ``Current conditions``
    serve only the most-recent observation and cannot satisfy a multi-year
    historical query. Without this fallback, such endpoints routinely keep a
    LLM-assigned 4.0 relevance that survives the 4.0 veto (regression case:
    USGS ``api.waterdata.usgs.gov/.../latest-daily`` against a 2010-2024 query).

    Returns:
      1.0  when either side is unparseable or the source's start covers the
           requested start (within a 2-year tolerance), or the source's end is
           open (".."), assumed to cover present day.
      0.7  when source starts up to ~5 years after the requested start (partial
           but missing significant history).
      0.4  when source starts more than ~5 years after the requested start
           (clear failure of a 10+ year history requirement, e.g. Databento
           2018+ vs a 2004-2024 query), OR temporal_coverage is missing and
           the name/description marks the source as latest-only against a
           multi-year request.
    """
    if not requested_range:
        return 1.0
    req_start, req_end = _extract_year_range(requested_range)
    if req_start is None:
        return 1.0

    # Primary path: structured comparison of source coverage vs request.
    if temporal_coverage:
        src_start, _ = _extract_year_range(temporal_coverage)
        if src_start is not None:
            start_gap = src_start - req_start
            if start_gap > 5:
                return 0.4
            if start_gap > 2:
                return 0.7
            return 1.0

    # Fallback: temporal_coverage absent. If the request spans 2+ years and
    # the name/description marks the source as latest-only, demote.
    req_span = (req_end - req_start) if req_end is not None else 0
    if req_span >= 2:
        text = " ".join(filter(None, [name, description]))
        if text and _LATEST_ONLY_RE.search(text):
            return 0.4
    return 1.0


# ──────────────────────────────────────────────────────────────────────
# Geographic fit (added 2026-05-22)
# ──────────────────────────────────────────────────────────────────────

# Normalize a few region/country aliases. Keep this small — the goal is
# to catch common Chinese-English variants users mix in queries; full
# geo normalization is a separate concern.
_GEO_ALIASES = {
    "china": {"china", "中国", "cn", "prc"},
    "us": {"us", "usa", "united states", "america", "美国"},
    "uk": {"uk", "united kingdom", "britain", "england", "英国"},
    "eu": {"eu", "europe", "european union", "欧洲"},
    "global": {"global", "world", "international", "worldwide", "全球"},
}


def _expand_geo_set(geo_strs: list[str] | None) -> set[str]:
    """Normalize a list of free-form geo strings into a canonical lower-cased
    set, with alias-resolution. Empty input → empty set."""
    if not geo_strs:
        return set()
    out: set[str] = set()
    for raw in geo_strs:
        s = str(raw).strip().lower()
        if not s:
            continue
        matched = False
        for canon, aliases in _GEO_ALIASES.items():
            if s in aliases:
                out.add(canon)
                matched = True
                break
        if not matched:
            out.add(s)
    return out


def score_geographic_fit(
    source_coverage: list[str] | None,
    requested_scope: list[str] | None,
) -> float:
    """Return a relevance multiplier in [0.5, 1.0] based on geographic fit.

    Currently embedded in the relevance LLM prompt as "single-jurisdiction
    sources must score noticeably lower". Pulling it out into a deterministic
    multiplier so an LLM that focuses on topic match can't silently let a
    geographic mismatch through.

    Rules:
      1.0   no requested scope (user didn't constrain) OR source unspecified
            (we can't tell; let LLM relevance carry the signal)
      1.0   "global" requested + source covers ≥2 distinct regions
      0.8   "global" requested + source covers exactly 1 region (partial)
      1.0   requested ⊆ source (source covers all required regions)
      0.8   requested ∩ source non-empty (partial coverage)
      0.5   requested ∩ source empty (clear geographic miss)
    """
    src = _expand_geo_set(source_coverage)
    req = _expand_geo_set(requested_scope)
    if not req or not src:
        return 1.0

    # "global" requested: prefer multi-region sources
    if "global" in req:
        non_global_src = src - {"global"}
        if "global" in src or len(non_global_src) >= 2:
            return 1.0
        if len(non_global_src) == 1:
            return settings.scoring.geographic_fit_partial_multiplier
        return settings.scoring.geographic_fit_mismatch_multiplier

    # Specific region(s) requested
    non_global_req = req - {"global"}
    overlap = non_global_req & src
    if not non_global_req:    # only "global" was filtered out
        return 1.0
    if overlap == non_global_req:
        return 1.0    # source covers all requested regions
    if overlap:
        return settings.scoring.geographic_fit_partial_multiplier
    return settings.scoring.geographic_fit_mismatch_multiplier


# ──────────────────────────────────────────────────────────────────────
# Schema coverage fit (added 2026-05-22)
# ──────────────────────────────────────────────────────────────────────


def score_schema_coverage_fit(
    fields_present: list[str] | None,
    target_schema: dict | None,
) -> float:
    """Return a relevance multiplier in [0.4, 1.0] based on schema-fit ratio.

    Replaces the LLM rubric's SCHEMA-FIT GUARDRAIL clause with an explicit
    deterministic check. LLMs occasionally cap themselves at 4 per the
    rubric, but just as often they overlook the guardrail and reward a
    topic-matching source whose fields are entirely orthogonal to the
    user's target schema.

    Inputs:
      - fields_present: surface-level field names extracted from the source
                        (embedded_spec.fields_present + metadata.fields_present)
      - target_schema: the requirement.target_schema dict (jsonschema-ish)

    Rules:
      1.0   no target_schema (user didn't specify) — let LLM carry the signal
      1.0   ≥30% overlap with target schema fields
      0.7   < 30% overlap (low coverage)
      0.4   zero overlap (no useful fields)
    """
    if not target_schema:
        return 1.0

    # Extract target field names from a JSON-Schema-ish dict.
    # parse_intent emits {"company_name": ..., "round": ..., "amount_usd": ...}
    # OR {"properties": {"company_name": ..., ...}} per schema.org convention.
    if "properties" in target_schema and isinstance(target_schema["properties"], dict):
        target_keys = set(k.lower() for k in target_schema["properties"].keys())
    else:
        target_keys = set(k.lower() for k in target_schema.keys()
                          if isinstance(k, str) and not k.startswith("_"))
    if not target_keys:
        return 1.0

    present = set((str(f) or "").strip().lower() for f in (fields_present or []))
    present.discard("")
    if not present:
        # Source declared no fields — judge can't verify schema fit;
        # don't penalize, let LLM relevance carry the signal.
        return 1.0

    # Strip common Schema.org/OpenGraph prefixes to normalize comparisons.
    def _local(name: str) -> str:
        if "#" in name:
            name = name.rsplit("#", 1)[-1]
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        return name

    present_local = {_local(f) for f in present}

    overlap = present_local & target_keys
    coverage = len(overlap) / len(target_keys)

    if coverage >= settings.scoring.schema_coverage_low_threshold:
        return 1.0
    if coverage > 0:
        return settings.scoring.schema_coverage_low_multiplier
    return settings.scoring.schema_coverage_zero_multiplier


# ──────────────────────────────────────────────────────────────────────
# Pre-Stage-A wrapper URL blacklist (added 2026-05-22)
# ──────────────────────────────────────────────────────────────────────

# URLs that we are confident are NOT data sources — they are either
# language-specific wrappers, package docs, or pure documentation. The
# relevance prompt already tells the LLM to cap these ≤ 4, but adding a
# hard pre-filter saves a Stage-A LLM call per match and prevents the
# LLM from forgetting the cap on a complex query.
#
# Conservative — only patterns where we're 100% sure. Anything ambiguous
# falls through to the regular LLM rubric.
_WRAPPER_URL_PATTERNS = (
    re.compile(r"^https?://rdrr\.io/"),
    re.compile(r"^https?://cran\.r-project\.org/web/packages/"),
    re.compile(r"^https?://cran\.r-project\.org/.*\.pdf$"),
    re.compile(r"^https?://pypi\.org/project/"),
    re.compile(r"^https?://www\.npmjs\.com/package/"),
    re.compile(r"^https?://[a-z0-9\-]+\.readthedocs\.io/"),
)


def is_wrapper_or_docs_url(url: str | None) -> bool:
    """True when the URL points to a language wrapper / package docs /
    readthedocs page — these aren't the underlying data source the user
    wants, just a description of how to access it from a specific stack."""
    if not url:
        return False
    u = url.strip().lower()
    return any(p.match(u) for p in _WRAPPER_URL_PATTERNS)
