"""Registered CKAN portal instances.

Each entry is auto-registered as a separate adapter at import time. To add a
new portal, append a dict with ``base_url`` and ``portal_name``. The adapter
will hit ``{base_url}/api/3/action/package_search`` by default; verify your
URL works with that path before adding (use ``scripts/probe_ckan_portals.py``).

Optional fields per entry:
    country: str          — full English country name (e.g. "United States",
                            "Germany"). Omit for international/supranational
                            portals (HDX, EU). Used by the registry worker
                            to skip country-specific portals when the
                            request's geographic_scope clearly doesn't
                            match — e.g. don't query Romanian / Estonian /
                            Slovak CKANs for "US stock data", which
                            previously wasted ~20s per query and starved
                            the budget for relevant portals.
    search_method: str    — "GET" (default) or "POST". EU uses POST.
    search_path: str      — defaults to "/api/3/action/package_search".
                            London uses legacy "/api/action/package_search"
                            (no /3/), EU uses "/api/hub/search/search".
    rows_param: str       — query parameter for result limit. Defaults to
                            "rows" (CKAN standard); EU expects "limit".
    result_inner_key: str — key under data["result"] containing the list
                            of packages. Defaults to "results"; London
                            (a non-standard variant) uses "result".
    notes: str            — free-form (ignored at runtime, for humans)

Coverage as of 2026-05-06: 17 verified-alive portals (down from 44 — 27 were
pruned after a batch probe found CKAN APIs had been deprecated, replaced, or
the domains had vanished). Re-run scripts/probe_ckan_portals.py periodically
to catch fresh breakage.
"""

from __future__ import annotations

CKAN_PORTALS: list[dict] = [
    # ============================================================
    # NATIONAL — North America
    # ============================================================
    {
        "base_url": "https://open.canada.ca/data/en",
        "portal_name": "Open Canada",
        "country": "Canada",
        "notes": "Canadian federal open government portal",
    },

    # ============================================================
    # NATIONAL — UK & Ireland
    # ============================================================
    {
        "base_url": "https://data.gov.uk",
        "portal_name": "data.gov.uk",
        "country": "United Kingdom",
        "notes": "UK government open data, ~50k datasets",
    },
    {
        "base_url": "https://data.gov.ie",
        "portal_name": "data.gov.ie (Ireland)",
        "country": "Ireland",
    },

    # ============================================================
    # SUPRANATIONAL — EU (custom POST API)
    # ============================================================
    {
        "base_url": "https://data.europa.eu",
        "portal_name": "EU Open Data Portal",
        # No `country` — supranational, always runs.
        "search_method": "POST",
        "search_path": "/api/hub/search/search",
        "rows_param": "limit",
        "notes": "Custom POST + JSON body; response is CKAN-shaped (count=20k+)",
    },

    # ============================================================
    # NATIONAL — Continental Europe
    # ============================================================
    {
        "base_url": "https://opendata.swiss",
        "portal_name": "opendata.swiss (Switzerland)",
        "country": "Switzerland",
    },
    {
        "base_url": "https://www.avoindata.fi/data/en",
        "portal_name": "avoindata.fi (Finland)",
        "country": "Finland",
    },
    {
        "base_url": "https://portal.opendata.dk",
        "portal_name": "portal.opendata.dk (Denmark)",
        "country": "Denmark",
    },
    {
        "base_url": "https://data.gov.lv",
        "portal_name": "data.gov.lv (Latvia)",
        "country": "Latvia",
    },
    {
        "base_url": "https://data.gov.si",
        "portal_name": "data.gov.si (Slovenia)",
        "country": "Slovenia",
    },

    # ============================================================
    # NATIONAL — Latin America
    # ============================================================
    {
        "base_url": "https://datos.gob.cl",
        "portal_name": "datos.gob.cl (Chile)",
        "country": "Chile",
    },
    {
        "base_url": "https://datos.gob.mx",
        "portal_name": "datos.gob.mx (Mexico)",
        "country": "Mexico",
    },
    {
        "base_url": "https://datos.gob.ar",
        "portal_name": "datos.gob.ar (Argentina)",
        "country": "Argentina",
    },

    # ============================================================
    # SUB-NATIONAL — UK (legacy CKAN variant)
    # ============================================================
    {
        "base_url": "https://data.london.gov.uk",
        "portal_name": "London Datastore",
        "country": "United Kingdom",
        "search_path": "/api/action/package_search",  # no /3/ — legacy path
        "result_inner_key": "result",  # non-standard: result.result not result.results
        "notes": "Legacy CKAN variant: `/3/` path returns HTML meta-refresh; result list is at result.result",
    },

    # ============================================================
    # SUB-NATIONAL — Germany
    # ============================================================
    {
        "base_url": "https://datenregister.berlin.de",
        "portal_name": "Berlin Open Data",
        "country": "Germany",
    },

    # ============================================================
    # SUB-NATIONAL — Canada provinces
    # ============================================================
    {
        "base_url": "https://catalogue.data.gov.bc.ca",
        "portal_name": "British Columbia (Canada)",
        "country": "Canada",
    },
    {
        "base_url": "https://data.ontario.ca",
        "portal_name": "Ontario (Canada)",
        "country": "Canada",
    },

    # ============================================================
    # INTERNATIONAL / SECTORAL
    # ============================================================
    {
        "base_url": "https://data.humdata.org",
        "portal_name": "HDX (UN OCHA Humanitarian Data Exchange)",
        # International — no `country` so it always runs.
        "notes": "20k+ humanitarian datasets, the gold standard for crisis data",
    },
]
