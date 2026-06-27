"""Shared URL regex patterns for type classification and portal detection."""

import re

# === File patterns: URLs that indicate downloadable files ===
FILE_PATTERNS = [
    re.compile(r"\.(csv|json|jsonl|xlsx|xls|parquet|xml|tsv|zip|gz|tar|bz2|7z)(\?|$)", re.I),
    re.compile(r"/download/", re.I),
    re.compile(r"/export/", re.I),
    re.compile(r"/bulk/", re.I),
    re.compile(r"/files/", re.I),
    re.compile(r"/datasets?/.*\.(csv|json|parquet)", re.I),
]

# === API patterns: URLs that indicate API endpoints ===
# Suppressed for documentation pages (DOC_HOST_PATTERNS / DOC_PATH_PATTERNS) —
# `/api/` inside developers.openai.com/api/docs/... is a doc URL, not an
# endpoint, and downstream API processing would just stamp placeholder
# auth_type/method on it.
API_PATTERNS = [
    re.compile(r"/api/", re.I),
    re.compile(r"/v[1-9]\d*/", re.I),
    re.compile(r"/rest/", re.I),
    re.compile(r"/graphql", re.I),
    re.compile(r"/swagger", re.I),
    re.compile(r"/openapi", re.I),
    re.compile(r"api\.\w+\.(com|org|io|net)", re.I),
    re.compile(r"/docs/?$", re.I),
]

# === Strong API signals: NOT suppressed by the doc-page guard ===
# These match URLs that ARE the canonical API entry point (api.* host) or
# describe the API in a way the user wants surfaced as `source_type=api`.
# Without this, an explicitly API-centric query against FRED/etc. returns
# zero api_sources because the canonical /api-documentation/rest-api page
# falls into the embedded bucket via the L3 LLM batch.
STRONG_API_PATTERNS = [
    # api.* host at any depth: api.fred.stlouisfed.org / api.openai.com / ...
    re.compile(r"^https?://api[\.\-]", re.I),
    # api-docs.* / api-reference.* host
    re.compile(r"^https?://api[-_]?(docs?|reference|spec|specs)\.", re.I),
    # Path describing API documentation/reference (the doc IS the API entry point)
    re.compile(r"/api[-_](documentation|reference|spec|specs)\b", re.I),
    re.compile(r"/(rest|graphql|json|web|http)[-_]?api(s|/|$)", re.I),
]

# === Documentation host/path indicators ===
# When a URL matches API_PATTERNS but ALSO matches one of these strong
# documentation signals, it is a static HTML doc page (e.g. developers.openai.com
# /api/docs/guides/rate-limits/) rather than a callable API endpoint. Without
# this guard, /api/, /rest/, etc. in the doc tree wrongly mark the URL as
# source_type=api and downstream auth_type / method fields are filled with
# "unknown" / "GET" placeholders that mislead users and inflate api_sources.
DOC_HOST_PATTERNS = [
    re.compile(r"^docs?\.", re.I),
    re.compile(r"^developers?\.", re.I),
    re.compile(r"^learn\.", re.I),
    re.compile(r"^help\.", re.I),
    re.compile(r"^support\.", re.I),
]

DOC_PATH_PATTERNS = [
    re.compile(r"/(docs?|documentation|guides?|reference|tutorials?|manual|articles?|posts?|learn)/", re.I),
]

# === Domain → type mapping for known sites ===
DOMAIN_TYPE_MAP: dict[str, list[str]] = {
    "kaggle.com": ["file"],
    "huggingface.co": ["file"],
    "zenodo.org": ["file"],
    "figshare.com": ["file"],
    "rapidapi.com": ["api"],
    "apis.guru": ["api"],
    # Known API-first vendors — their root URL is a marketing page but the
    # product is the API. Without this mapping the HEAD probe sees text/html
    # and misclassifies them as EMBEDDED, after which the embedded cascade
    # extracts og:title/og:description as the "schema" (see Polygon.io regression
    # where rdfa Open Graph tags were surfaced as the data fields_present).
    "polygon.io": ["api"],
    "alphavantage.co": ["api"],
    "tiingo.com": ["api"],
    "iexcloud.io": ["api"],
    "iex.cloud": ["api"],
    "finnhub.io": ["api"],
    "twelvedata.com": ["api"],
    "quandl.com": ["api"],
    "data.nasdaq.com": ["api"],
    "financialmodelingprep.com": ["api"],
    "intrinio.com": ["api"],
    "marketstack.com": ["api"],
    # USGS NWIS is the canonical hydrology data API. The host serves an HTML
    # landing page describing both `/nwis/dv/` (daily values) and `/nwis/iv/`
    # (instantaneous values) endpoints — without this mapping, the L2 HEAD
    # probe sees text/html and the URL falls into the EMBEDDED bucket, after
    # which the embedded cascade extracts boilerplate JSON-LD instead of
    # surfacing it as the api source the user asked for.
    "waterservices.usgs.gov": ["api"],
    "waterqualitydata.us": ["api"],
    # waterdata.usgs.gov is the canonical USGS water-data front-end
    # (umbrella for /nwis/uv, /nwis/dv, /nwis/iv, /nwis/wqx); the bare
    # host returns HTML so without this mapping the L2 HEAD probe routes
    # the URL to EMBEDDED. When Firecrawl is unavailable (DNS-failure /
    # rate-limited / self-hosted instance down), every embedded scrape
    # raises and the candidate is silently dropped at the embedded
    # processor's `Both HTML and Markdown empty` guard — wiping a
    # known_authoritative_sources entry from the report. Routing to the
    # API processor instead (HEAD probe + api_metadata enrichment) keeps
    # the candidate alive even when the scraping subsystem is down.
    "waterdata.usgs.gov": ["api"],
    # water.noaa.gov hosts the NOAA National Water Center / National
    # Water Prediction Service — the NWM dashboard, river observation
    # maps, and forecast-product index. parse_intent commonly emits
    # "NOAA National Water Model" which the alias table resolves to
    # this host. The bare host returns HTML so the L2 HEAD probe routes
    # it to EMBEDDED, where it gets dropped by Firecrawl outages just
    # like waterdata.usgs.gov above. Treat as API entry-point so it
    # survives the embedded-cascade dependency on Firecrawl.
    "water.noaa.gov": ["api"],
    # CDEC serves both an HTML landing at cdec.water.ca.gov AND a programmatic
    # web-services surface at cdec.water.ca.gov/dynamicapp/req/{CSVDataServlet,
    # JSONDataServlet} (CSV/JSON output, no auth). Without this mapping, the
    # bare host falls through L2 HEAD probe → text/html → EMBEDDED, so the
    # candidate enters the embedded subgraph and surfaces with no api_data /
    # data_format / temporal_coverage / license fields — making it score
    # below Stage-A judge's 6.0 threshold and drop out of the report despite
    # being a canonical California state-level streamflow / water-quality
    # source. Mirrors the waterservices.usgs.gov pattern above.
    "cdec.water.ca.gov": ["api"],
    "amazon.com": ["embedded"],
    "wikipedia.org": ["embedded"],
    "imdb.com": ["embedded"],
    "yelp.com": ["embedded"],
    "tripadvisor.com": ["embedded"],
    "zillow.com": ["embedded"],
    "crunchbase.com": ["embedded"],
    # NOTE: do not blanket-map "github.com" to ["api","file"]. A GitHub repo
    # landing page is just HTML — it is neither a REST endpoint nor a file.
    # The patterns above already catch the real cases:
    #   api.github.com/... → API_PATTERNS via the api.<host> rule
    #   .../raw/... or .csv/.json suffixes → FILE_PATTERNS
    # Bare repo URLs fall through to the HEAD probe and land on EMBEDDED,
    # which avoids surfacing the same URL twice (once as api, once as file)
    # and wasting two result slots on a single low-information landing page.
}

# === Portal patterns ===
PORTAL_URL_PATTERNS = [
    re.compile(r"^https?://data\.", re.I),
    re.compile(r"^https?://datos\.", re.I),
    re.compile(r"^https?://opendata\.", re.I),
    re.compile(r"/portal/?$", re.I),
    re.compile(r"/datacatalog/?$", re.I),
    re.compile(r"/opendata/?$", re.I),
]

PORTAL_SIGNAL_WORDS = [
    "open data portal", "data catalog", "data repository",
    "dataset collection", "browse datasets", "explore data",
    "数据门户", "数据目录", "开放数据",
]

# === Known data portals whitelist ===
KNOWN_PORTALS: set[str] = {
    "data.gov", "data.gov.uk", "data.europa.eu", "data.gov.au",
    "data.gov.sg", "data.gov.in", "data.gov.ie", "data.gov.be",
    "data.worldbank.org", "data.imf.org", "data.un.org", "data.oecd.org",
    "data.humdata.org", "data.unicef.org",
    "kaggle.com", "huggingface.co", "zenodo.org", "figshare.com",
    "fred.stlouisfed.org", "earthdata.nasa.gov", "copernicus.eu",
    "datasetsearch.research.google.com", "dataverse.harvard.edu",
    "registry.opendata.aws", "datahub.io",
    "ourworldindata.org", "tradingeconomics.com",
    "opendatasoft.com", "data.nasa.gov", "data.noaa.gov",
    "data.census.gov", "data.bls.gov", "data.cms.gov",
    "datos.gob.es", "datos.gob.mx", "dados.gov.br",
    "data.go.jp", "data.go.kr", "data.go.th",
    "data.gov.cn", "data.stats.gov.cn",
    "data.gov.hk", "data.gov.tw",
    "knoema.com", "statista.com",
}
