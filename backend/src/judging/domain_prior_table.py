"""Domain authority prior table — maps 500+ known domains to base authority scores.

Layer 1 of authority scoring. Human-maintained, stored in PostgreSQL for
runtime updates. This file provides the default in-memory table.
"""

# Domain → base authority score (0-10)
# Higher = more authoritative/trustworthy
DOMAIN_AUTHORITY: dict[str, float] = {
    # === Government & International Organizations (9-10) ===
    "data.gov": 9.5,
    "data.gov.uk": 9.5,
    "data.europa.eu": 9.5,
    "data.worldbank.org": 9.5,
    "data.imf.org": 9.5,
    "data.un.org": 9.5,
    "data.oecd.org": 9.5,
    "fred.stlouisfed.org": 9.5,
    "alfred.stlouisfed.org": 9.5,
    "stlouisfed.org": 9.5,
    # Parent publisher domains so authority-by-publisher recognizes sibling
    # subdomains (api.*, research.*, data.*) instead of falling back to the
    # LLM verdict on empty metadata, which had been giving the same publisher
    # wildly inconsistent scores (9.5 vs 1.0) across siblings.
    "bls.gov": 9.5,
    "bea.gov": 9.5,
    "federalreserve.gov": 9.5,
    "data.census.gov": 9.5,
    "data.bls.gov": 9.5,
    "data.noaa.gov": 9.0,
    # NOAA / USGS hydrology and environmental archives — the canonical
    # publishers for streamflow, water-quality, and climate data.
    "noaa.gov": 9.0,
    "ncei.noaa.gov": 9.5,
    "water.noaa.gov": 9.5,
    "usgs.gov": 9.5,
    "waterservices.usgs.gov": 9.5,
    "waterwatch.usgs.gov": 9.5,
    "waterqualitydata.us": 9.0,
    "hydroshare.org": 8.0,
    # California state water authorities. Without an explicit prior, the
    # 4-label `cdec.water.ca.gov` host falls into the "Unknown domain"
    # path → base 5.0 + .gov bonus 2.0 = 7.0 with a misleading
    # "Unknown domain (.gov/.edu +2.0 TLD bonus)" rationale that
    # contradicts itself. Adding the registrable parents lets the
    # suffix-match in get_domain_authority() lift CDEC, DWR, and SWRCB
    # to a correct 9.0 with a clean "Domain prior" rationale.
    "water.ca.gov": 9.0,
    "waterboards.ca.gov": 9.0,
    # Colorado state water authorities. Same shape as the California
    # entries above: parse_intent's known_authoritative_sources path
    # gives them a 7.0 floor (via is_known_authoritative=True), but a
    # 7.0 sub-score for the state's primary water-rights / streamflow
    # publisher reads as inverted next to data.gov's 9.5 on the same
    # query. Adding domain priors at 9.0 puts them on par with
    # data.noaa.gov (also a 9.0 federal hydrology catalog) so the
    # authority sub-score reflects the publisher quality rather than
    # the unknown-domain fallback.
    "dwr.colorado.gov": 9.0,
    "dnr.colorado.gov": 9.0,
    "cwcb.colorado.gov": 9.0,
    "cdss.colorado.gov": 9.0,
    # The HydroBase REST API is hosted on the legacy `dwr.state.co.us`
    # domain — a separate registrable parent from `colorado.gov`. The
    # canonical `/Rest/GET/api/v2/` endpoint lives here, so this is the
    # actual primary publisher for HydroBase queries.
    "dwr.state.co.us": 9.5,
    # US Army Corps of Engineers — operates major dams & reservoirs nationwide.
    # `.army.mil` is a restricted DoD TLD but does NOT match the .gov/.edu
    # bonus path in authority.py, so without an explicit prior all USACE
    # district subdomains (spk.usace.army.mil, nwd.usace.army.mil, etc.)
    # fall to the unknown base 5.0.
    "usace.army.mil": 8.5,
    "data.nasa.gov": 9.0,
    "earthdata.nasa.gov": 9.0,
    # Climate triad parent/sibling domains so GISTEMP, NASA earthdata-CMR,
    # and WMO climate hosts are recognized instead of falling to the
    # "Unrecognized publisher" LLM verdict.
    "nasa.gov": 9.0,
    "giss.nasa.gov": 9.0,
    "wmo.int": 9.5,
    "data.who.int": 9.5,
    # Parent who.int + US/EU health-data portals — without explicit entries
    # the WHO homepage and CDC/NIH/HHS hosts fall to the LLM fallback that
    # produces inconsistent scores.
    "who.int": 9.5,
    "healthdata.gov": 9.0,
    "cdc.gov": 9.5,
    "nih.gov": 9.5,
    "hhs.gov": 9.0,
    "stats.oecd.org": 9.0,
    "sdmx.oecd.org": 9.5,
    "oecd.org": 9.0,
    "oecd-ilibrary.org": 8.5,
    "imf.org": 9.0,
    "worldbank.org": 9.0,
    "ec.europa.eu": 9.0,
    "eurostat.ec.europa.eu": 9.5,
    "ecdc.europa.eu": 9.5,
    "ema.europa.eu": 9.5,
    "efsa.europa.eu": 9.0,
    "eea.europa.eu": 9.0,
    "europa.eu": 8.5,
    "data.unicef.org": 9.0,
    "unicef.org": 8.5,
    "data.humdata.org": 8.5,
    # Canada — central bank, statistics, open data
    "bankofcanada.ca": 9.5,
    "statcan.gc.ca": 9.5,
    "www150.statcan.gc.ca": 9.5,
    "open.canada.ca": 9.5,
    "data.gc.ca": 9.5,
    "canada.ca": 9.0,
    "fin.gc.ca": 9.0,

    # === Academic & Research (8-9) ===
    "openalex.org": 8.5,
    "api.openalex.org": 8.5,
    "api.semanticscholar.org": 8.5,
    "arxiv.org": 9.0,
    "pubmed.ncbi.nlm.nih.gov": 9.0,
    "crossref.org": 8.5,
    "scholar.google.com": 8.0,
    "dataverse.harvard.edu": 8.5,
    "data.mendeley.com": 7.5,

    # === Dataset Platforms (7-9) ===
    "huggingface.co": 8.5,
    "kaggle.com": 8.0,
    "zenodo.org": 8.0,
    "figshare.com": 7.5,
    "datahub.io": 7.0,
    "registry.opendata.aws": 8.0,
    "datasetsearch.research.google.com": 8.0,

    # === Finance (7-9) ===
    "sec.gov": 9.5,
    "finance.yahoo.com": 7.0,
    "polygon.io": 7.5,
    # The Alpha Vantage canonical domain is alphavantage.co (no dash). The
    # earlier "alpha-vantage.co" entry never matched the real host so the
    # source kept landing on the unknown-domain path.
    "alphavantage.co": 7.0,
    "quandl.com": 7.5,
    "tradingeconomics.com": 7.5,
    "investing.com": 6.5,
    # Primary US exchanges + research authority. Without explicit priors
    # these primary publishers were flagged "unrecognized publisher
    # (nyse.com / data.nasdaq.com / crsp.org); limited authority signals"
    # despite being the canonical sources finance queries route to.
    "nyse.com": 9.0,
    "nasdaq.com": 9.0,
    "data.nasdaq.com": 8.5,
    # CRSP — Center for Research in Security Prices (U. of Chicago Booth);
    # the academic gold standard for survivorship-bias-free US equity data.
    "crsp.org": 9.0,
    # Established commercial historical-price providers. Mid-tier authority:
    # widely cited in fintech but not primary publishers like NYSE/Nasdaq.
    "tiingo.com": 7.0,
    "eodhistoricaldata.com": 6.5,
    "eodhd.com": 6.5,
    "stooq.com": 6.0,
    "quantquote.com": 6.5,
    "firstratedata.com": 6.5,
    "norgatedata.com": 7.0,
    # Commercial aggregators / dashboards (mid-tier — not primary sources)
    "ycharts.com": 6.0,
    "tradingview.com": 5.5,
    "macrotrends.net": 5.5,
    "marketwatch.com": 6.5,
    "ceicdata.com": 6.0,
    "cbonds.com": 6.0,
    "bondstats.org": 5.5,

    # === Tech / API Directories (7-8) ===
    "github.com": 7.5,
    "apis.guru": 7.5,
    "rapidapi.com": 6.5,
    "programmableweb.com": 6.5,
    "any-api.com": 5.5,
    "public-apis.io": 6.0,

    # === LLM / AI API Vendor Official Docs (8-9) ===
    # Parent domains catch developers.*, platform.*, docs.*, api.* subdomains
    # via the suffix-match in get_domain_authority().
    "openai.com": 8.5,
    "anthropic.com": 8.5,
    "claude.com": 8.5,
    "ai.google.dev": 8.5,
    "cloud.google.com": 8.0,
    "developers.google.com": 8.0,
    "mistral.ai": 8.0,
    "cohere.com": 8.0,
    "x.ai": 7.5,
    "groq.com": 7.5,
    "deepseek.com": 7.5,
    "together.ai": 7.5,
    "replicate.com": 7.5,
    "openrouter.ai": 7.0,
    "fireworks.ai": 7.0,
    "perplexity.ai": 7.0,
    "cerebras.ai": 7.0,
    "moonshot.cn": 7.0,
    "aws.amazon.com": 8.0,
    "azure.microsoft.com": 8.0,
    "learn.microsoft.com": 8.0,
    "docs.microsoft.com": 8.0,
    "developer.mozilla.org": 8.5,
    "developers.cloudflare.com": 8.0,

    # === Reference & Knowledge (7-9) ===
    "wikipedia.org": 7.5,
    "wikidata.org": 8.5,
    "dbpedia.org": 7.5,
    "ourworldindata.org": 8.5,
    "statista.com": 7.5,
    "knoema.com": 7.0,

    # === Geo & Environmental (8-9) ===
    "copernicus.eu": 9.0,
    "openstreetmap.org": 8.0,
    "naturalearthdata.com": 7.5,

    # === News & Social (5-7) ===
    "gdeltproject.org": 7.0,
    "commoncrawl.org": 7.0,
    "reddit.com": 4.5,
    "twitter.com": 4.5,

    # === Commercial Data (5-7) ===
    "crunchbase.com": 7.0,
    "glassdoor.com": 5.5,
    "indeed.com": 5.5,
    "yelp.com": 5.0,
    "tripadvisor.com": 5.0,
    "zillow.com": 6.0,
}


def get_domain_authority(domain: str) -> float | None:
    """Look up base authority score for a domain.

    Returns None if domain is not in the prior table (→ use Layer 2/3).
    """
    domain = domain.lower().replace("www.", "")

    # Exact match
    if domain in DOMAIN_AUTHORITY:
        return DOMAIN_AUTHORITY[domain]

    # Check if any known domain is a suffix of this domain
    for known, score in DOMAIN_AUTHORITY.items():
        if domain.endswith(f".{known}") or domain == known:
            return score

    return None
