"""Relevance scoring rubric — strict prompt template for LLM-based relevance evaluation."""

RELEVANCE_RUBRIC = """Score the RELEVANCE of this data source to the user's data need on a 0-10 scale.

USER REQUIREMENT:
- Query: {query}
- Domain: {domain}
- Desired formats: {formats}
- Geographic scope: {geo_scope}
- Temporal range: {temporal}
- Target schema fields: {target_fields}

DATA SOURCE:
- Name: {source_name}
- URL: {source_url}
- Type: {source_type}
- Description: {description}
- Data format: {data_format}
- Coverage: {coverage}
- Source fields actually present: {source_fields}
- Sample: {sample}

SCORING RUBRIC (be strict):
  10: PERFECT MATCH — exactly provides the needed domain, type, geography, and time range
  8-9: STRONG MATCH — main dimensions match, minor constraint gaps (e.g., slightly different time range)
  6-7: PARTIAL MATCH — core topic is right, but at least one key constraint doesn't fit
  4-5: WEAK MATCH — topic is related but data needs significant transformation/filtering
  2-3: BARELY RELATED — only tangentially related to the need
  0-1: IRRELEVANT — different topic entirely

SCHEMA-FIT GUARDRAIL: when "Source fields actually present" is provided and
shares zero meaningful overlap with the target schema fields, cap the score at 4.
Example: query asks for OHLCV+dividend+split fields, but the source's actual
fields are ["Added/Removed", "PERMNO", "Company", "Ticker", "SP500 Start",
"SP500 End"] (a constituent-change list, not a price time series) → ≤4.
Do not assume the source has additional fields beyond what's listed; only og:*
or http://ogp.me/* fields are page-level marketing metadata, not data fields.

FORMAT-MISMATCH HARD CAP: When desired formats include "json", "api", "rest", or
"csv", language-specific wrapper libraries and documentation files are NOT the
canonical endpoint and MUST be capped at relevance ≤ 4. This applies to:
  - R packages and their docs (rdrr.io/cran/*, cran.r-project.org/web/packages/*)
  - PDF/HTML manuals that describe an API but require a different language stack
  - SDK READMEs that do not host the underlying data themselves
The user wants to call the REST endpoint directly; these are wrappers around it,
not the endpoint itself. Score them ≤ 4 even if they describe the right dataset.

CRITICAL: In your rationale, briefly cover (in 2-4 short sentences total): which dimensions match,
which dimensions do not match (especially schema-fit when fields are listed), and one specific reason
this beats or trails a generic search result. Cite concrete facts from the source description rather
than restating the domain.

Output JSON:
{{"score": 7.5, "rationale": "Matches domain (finance) and format (API). Geographic coverage is global but user needs US-specific. Time range matches (2014-2025) and provides 6/8 target schema fields. Authoritative primary publisher rather than a generic aggregator."}}
"""


# Schema.org / OpenGraph / RDFa property names that describe the page or the
# dataset's *catalog metadata* (title, publisher, license, citation links) but
# are NOT the dataset's actual data columns. Tier-0 JSON-LD/microdata/RDFa
# extraction surfaces these as `fields_present`; if we hand them to the
# relevance LLM as if they were data columns, the schema-fit guardrail
# fires and an authoritative dataset (e.g. data.cdc.gov COVID-19 hospital
# metrics, with JSON-LD type=Dataset) gets scored 1/10 because its surfaced
# fields (name, description, keyword, distribution, ...) share zero overlap
# with the user's target schema (date, state, hospitalized_confirmed, ...).
# That triggers the relevance<4 hard veto in SourceScores.aggregate, which
# is what collapsed 105 → 0 in the COVID-19 query.
_PAGE_METADATA_FIELDS: frozenset[str] = frozenset({
    # Schema.org Dataset/CreativeWork catalog metadata
    "name", "alternatename", "description", "url", "sameas", "identifier",
    "keyword", "keywords", "creator", "publisher", "author", "contributor",
    "license", "isaccessibleforfree", "ispartof", "haspart", "mainentity",
    "mainentityofpage", "distribution", "encoding", "encodingformat",
    "datepublished", "datemodified", "datecreated", "dateissued",
    "version", "citation", "funder", "sponsor", "provider",
    "spatialcoverage", "temporalcoverage", "variablemeasured",
    "subjectof", "additionaltype",
    # Schema.org Service / Organization / DataCatalog metadata. These describe
    # *what the catalog is* (a Service that provides search; an Organization
    # with a parentOrganization; a DataCatalog with availableChannel endpoints)
    # rather than the data columns of any individual dataset. Iter 11
    # regression: HydroShare's JSON-LD (@type=[Service, Organization,
    # DataCatalog]) surfaced fields_present=[additionalType, category,
    # parentOrganization, publishingPrinciples, availableChannel, ...] which
    # share zero overlap with the user's data schema (watershed_id, station_id,
    # timestamp, ...) — the relevance LLM dutifully applied the schema-fit cap
    # and a legitimate authoritative catalog homepage scored only 7. These are
    # service/catalog-level descriptors, never data columns.
    "category", "parentorganization", "publishingprinciples",
    "availablechannel", "member", "audience", "affiliation",
    "serviceurl", "serviceoutput", "servicearea", "servicetype",
    "servicelocation", "parentservice", "producesdata", "providedby",
    "award", "brand", "slogan", "numberofemployees", "dissolutiondate",
    "foundinglocation", "department", "suborganization",
    "ownershipfundinginfo", "diversitypolicy", "ethicspolicy",
    # Page chrome / branding
    "logo", "image", "thumbnail", "thumbnailurl", "headline",
    "breadcrumb", "potentialaction", "searchaction", "inlanguage",
    # Generic WebPage/Article statistics — counters about *the page itself*
    # (length, comment count, reading time) rather than any data record.
    # Iter 12 regression: USGS Water Services homepage Tier-0 microdata
    # surfaced fields_present=[name, description, dateModified, wordCount,
    # image] — the first four are stripped above, but wordCount slipped
    # through as a "real field", making _has_actionable_spec return True
    # and skipping the format_fit penalty even though the page hosts only
    # a generic schema.org/WebPage description (no Dataset markup).
    "wordcount", "numberofitems", "numberofpages", "commentcount",
    "interactionstatistic", "timerequired", "readingtime",
    # Contact / org boilerplate
    "telephone", "email", "address", "contactpoint", "founder",
    "foundingdate", "legalname",
    # OpenGraph properties (og: prefix is stripped by some extractors)
    "type", "site_name", "locale", "title",
    # OpenGraph properties whose local-name forms are NOT covered by the
    # bare-noun entries above (modified_time / published_time / updated_time
    # / image:url / image:width / image:height / video:url / etc.). Iter 13
    # regression: ECDC opendata.ecdc.europa.eu RDFa extraction surfaced
    # fields_present=['http://ogp.me/ns/article#modified_time',
    # 'https://ogp.me/ns#description', 'https://ogp.me/ns#image:url', ...].
    # After normalization (strip prefix up to last '#') the set of remaining
    # locals is {modified_time, published_time, description, image, image:url,
    # site_name, title, type, updated_time, url}. Only image:url / modified_time
    # / published_time / updated_time were missing from the bare-noun set.
    "modified_time", "published_time", "updated_time", "release_date",
    "image:url", "image:width", "image:height", "image:alt", "image:type",
    "video:url", "video:width", "video:height", "video:type",
    "audio:url", "audio:type",
    "see_also", "first_name", "last_name", "username", "gender",
})


# OpenGraph / RDFa namespace prefix patterns. A `fields_present` entry matching
# any of these (case-insensitive) is page-level marketing metadata regardless
# of its local name — these namespaces never carry data columns. Detected
# alongside `_PAGE_METADATA_FIELDS` so we filter both the bare-noun forms
# (`description`, `title`) and the fully-qualified URI forms
# (`https://ogp.me/ns#description`, `http://ogp.me/ns/article#modified_time`).
_PAGE_METADATA_NAMESPACE_PREFIXES: tuple[str, ...] = (
    "http://ogp.me/",
    "https://ogp.me/",
    "http://opengraphprotocol.org/",
    "https://opengraphprotocol.org/",
    "og:",
    "article:",
    "fb:",
    "twitter:",
)


def _normalize_field_name_for_metadata_lookup(field_name: str) -> str:
    """Return the lowercased local name of a fields_present entry.

    RDFa / JSON-LD / microdata extractors surface schema property names as
    fully-qualified URIs (e.g. `https://ogp.me/ns#description`,
    `http://schema.org/name`). The `_PAGE_METADATA_FIELDS` set holds short
    local names (`description`, `name`); without normalization the lookup
    misses every URI form and these page-metadata fields slip through as
    if they were data columns, triggering the schema-fit guardrail on
    legitimate authoritative sources (regression case: ECDC opendata.ecdc
    .europa.eu RDFa extraction → OG/RDFa-only fields → relevance=3.0).

    The local name is whatever follows the last `#` or `/` in the URI;
    OG-style `og:title` / `article:modified_time` fragments lose their
    `og:` / `article:` prefix here.
    """
    name = (field_name or "").strip().lower()
    if not name:
        return name
    if "#" in name:
        name = name.rsplit("#", 1)[-1]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith(("og:", "article:", "fb:", "twitter:")):
        name = name.split(":", 1)[1]
    return name


def is_page_metadata_field(field_name: str) -> bool:
    """True when a `fields_present` entry is page-level marketing/catalog
    metadata rather than a real data column.

    Used both by the relevance rubric (filter the prompt's source_fields list)
    and by the actionable-spec guardrail (don't count metadata-only fields as
    structural evidence of a real dataset). Centralizes the URI-normalization
    + namespace-prefix logic so both call sites stay in sync.
    """
    if not field_name:
        return True
    f = str(field_name).strip().lower()
    if not f:
        return True
    for prefix in _PAGE_METADATA_NAMESPACE_PREFIXES:
        if f.startswith(prefix):
            return True
    if _normalize_field_name_for_metadata_lookup(f) in _PAGE_METADATA_FIELDS:
        return True
    return False


def _normalize_field_name(field: object) -> str:
    """Strip URI prefix / fragment from a fields_present entry so set lookups work.

    RDFa / microdata extruct extraction emits full URIs for property names
    (e.g. ``https://ogp.me/ns#title``, ``http://schema.org/name``) while
    JSON-LD typically yields bare names (``title``, ``name``). Without
    normalization, frozenset lookups against the bare lowercase tokens in
    ``_PAGE_METADATA_FIELDS`` silently fail for RDFa-extracted sources —
    which is how the ``usgs.gov`` homepage scrape (fields_present all of
    the form ``https://ogp.me/ns#*``) bypassed the page-metadata filter,
    looked actionable, and surfaced as a "useless homepage scrape" data
    source despite the relevance LLM correctly scoring it 3/10. Mirrors
    ``_normalize_schema_type`` in deterministic.py.

    Used by stage_b.py for matching against ``_DATASET_SIGNAL_FIELDS`` —
    a separate frozenset of dataset-signal property names which need the
    same URI→bare-name normalization to match RDFa-extracted entries.
    """
    s = str(field).strip().lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if "#" in s:
        s = s.rsplit("#", 1)[-1]
    return s


def _extract_source_fields(source: dict) -> str:
    """Pull the actual schema/columns the source exposes from its type spec.

    Embedded sources carry fields_present in embedded_spec; file sources carry
    column_headers in file_spec; APIs typically don't enumerate fields ahead of
    time. We surface this so the LLM can detect schema-mismatch (e.g. a WRDS
    constituent-change table being scored as if it had OHLCV columns).

    Embedded `fields_present` from Tier-0 JSON-LD/microdata extraction is
    filtered: page-level Schema.org metadata field names (name, description,
    keyword, distribution, telephone, breadcrumb, ...) are stripped because
    they are *catalog metadata about the dataset*, not the dataset's data
    columns. Surfacing them to the LLM as if they were columns triggers the
    schema-fit guardrail on real datasets whose actual columns we couldn't
    enumerate. File `column_headers` are NOT filtered — those come from real
    CSV/Parquet headers and ARE the data columns.
    """
    embedded_fields: list[str] = []
    file_fields: list[str] = []

    embedded = source.get("embedded_spec") or {}
    if isinstance(embedded, dict):
        ef = embedded.get("fields_present") or []
        if isinstance(ef, list):
            for f in ef:
                fname = str(f)
                if not is_page_metadata_field(fname):
                    embedded_fields.append(fname)

    fspec = source.get("file_spec") or {}
    if isinstance(fspec, dict):
        cols = fspec.get("column_headers") or []
        if isinstance(cols, list):
            file_fields.extend(str(c) for c in cols)

    fields = embedded_fields + file_fields
    if not fields:
        # REST APIs serve dynamic responses based on request parameters and do
        # not enumerate their column schema in static metadata. Telling the LLM
        # "cannot verify schema-fit" without context makes it cap exact-named
        # REST API sources at 8/10 ("STRONG MATCH with minor gap") instead of
        # the deserved 9-10 ("PERFECT MATCH") — the LLM cites the missing
        # enumeration as a reason for conservative scoring (regression: DWR
        # HydroBase scored 8.0 with rationale "Schema-fit cannot be verified
        # from metadata since fields are not enumerated"). Disambiguate the
        # API case so the LLM treats the guardrail as not applicable rather
        # than as an unverifiable risk.
        source_type = str(source.get("source_type") or "").lower()
        if source_type == "api":
            return (
                "n/a — REST API serves dynamic query responses; schema-fit "
                "guardrail does not apply (do not penalize relevance for "
                "missing field enumeration)"
            )
        return "not enumerated (cannot verify schema-fit from metadata alone)"
    # Cap to keep prompt bounded
    if len(fields) > 30:
        fields = fields[:30] + [f"... and {len(fields) - 30} more"]
    return ", ".join(fields)


def format_relevance_prompt(
    requirement: dict,
    source: dict,
) -> str:
    """Format the relevance scoring prompt with requirement and source details."""
    return RELEVANCE_RUBRIC.format(
        query=requirement.get("original_query", ""),
        domain=requirement.get("domain", "unknown"),
        formats=", ".join(requirement.get("desired_formats", ["any"])),
        geo_scope=", ".join(requirement.get("geographic_scope", ["global"])),
        temporal=requirement.get("temporal_range", "any"),
        target_fields=", ".join(
            requirement.get("target_schema", {}).get("properties", {}).keys()
        ) if requirement.get("target_schema") else "not specified",
        source_name=source.get("name", ""),
        source_url=source.get("url", ""),
        source_type=source.get("source_type", ""),
        description=source.get("description", "")[:300],
        data_format=", ".join(source.get("data_format", [])),
        coverage=f"Geo: {source.get('geographic_coverage', [])}, Time: {source.get('temporal_coverage', 'unknown')}",
        source_fields=_extract_source_fields(source),
        sample=source.get("sample_excerpt", "none")[:200] if source.get("sample_excerpt") else "none",
    )
