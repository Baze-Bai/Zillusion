<!--
Vendored from the Zillusion repo's curated `portal-detection` skill. The
heuristics below are tool-agnostic judgment calls about whether a URL is a
PORTAL (expandable) or a LEAF (terminal). The original doc names two backend
tools you do NOT have in standalone mode — translate them like this:

  • `firecrawl_map`  (the TRUE-PORTAL branch) → in standalone mode means:
    read the site's `sitemap.xml`, or WebFetch the publisher root, or run a
    `site:domain` WebSearch. You want the URL *skeleton*; query strings are
    ignored by all three mechanisms (that's the whole point — and the trap).

  • `crawl_list_tree` (the INTERIOR-LIST branch) → in standalone mode means:
    RENDER the page with Playwright (so `?search=`/`?q=` filters actually take
    effect) and extract the links a real user would see. Use httpx first if the
    listing is server-rendered; reach for Playwright only when it's JS-built.

Everything else (the signal tables, the known-portals list, the examples) is
reproduced verbatim because the wording is load-bearing.
-->

# Portal vs Leaf Detection — Curated Heuristics

## Purpose

Quickly decide:
1. **Is this a portal/list page (expandable) or a leaf (terminal data resource)?**
2. **If expandable, which technique: static-skeleton (`firecrawl_map`) or render-then-extract (`crawl_list_tree`)?** (see header for the standalone mapping)

Use these heuristics BEFORE expanding a URL — it avoids the well-known
query-string-loss trap (a `site:`/sitemap map of a filtered URL returns the
whole catalog, not your filtered subset).

## When to use

After you fetch a page (WebFetch / httpx) and have its markdown/HTML, before
deciding whether and how to expand the URL.

## TRUE PORTAL vs INTERIOR LIST PAGE

Both have "many same-shape sub-pages" — the difference is in HOW you should
enumerate them.

| Signal | TRUE PORTAL → static-skeleton (`firecrawl_map`) | INTERIOR LIST → render-then-extract (`crawl_list_tree`) |
|---|---|---|
| URL has `?search=`/`?q=`/`?filter=`/`?category=` | NO | **YES** |
| Path depth | shallow (`/`, `/dataset`, `/datasets`) | mid-deep (`/hotels/list`, `/search`, `/category/x`) |
| Sub-domain pattern | publisher root (`catalog.data.gov`) | search/list subdomain (`search.jd.com`, `s.taobao.com`) |
| Renders the same regardless of query | YES | **NO** (visible content depends on query) |
| Has a `sitemap.xml` that covers what you want | YES | **NO** (sitemap won't list filter variants) |

If ≥ 2 cells fall in the right-hand column → render-then-extract.
Otherwise → static-skeleton.

**Why this matters**: the static-skeleton technique ultimately runs `site:URL`
against a search backend or reads `sitemap.xml`. Both mechanisms IGNORE the
URL's query string. If you point it at `huggingface.co/datasets?search=sentiment`
you get back 200 random HF datasets — none of them sentiment-related.
Render-then-extract loads the page in a real browser, so the query filter takes
effect, and you get the URLs a user would actually see.

## Portal signals (count ≥ 3 of these → likely portal)

- `## Datasets` / `## Browse` / `## Catalog` / `## All datasets` headings
- Many repeated `[Title](/dataset/{slug})` link patterns (≥ 10)
- "X datasets matching" / "Showing N results" copy
- Faceted-filter UI: "Filter by: format, organization, license"
- Sitemap-like sections: `## A` `## B` `## C` listings
- Pagination links: "Next page", "Page 2 of 50"
- Search-results page indicator (`?q=` or `?search=` in URL)

## Leaf signals (any one strong) → NOT a portal

- Single dataset name in `<h1>` + `## Download` section with file links
- `## API Reference` / `## Endpoint` / `## Authentication` with code blocks
- One JSON-LD `Dataset` block with `distribution` URLs (parse the
  `application/ld+json` script to confirm)
- News article / blog post format
- One main data table (HTML `<table>`) covering the whole page
- File extensions in URL: `.csv`, `.json`, `.xlsx` ⇒ definitely leaf

## Known TRUE PORTALS (use static-skeleton)

These are publisher-root catalogs — `sitemap.xml` covers their inventory,
no JS-rendered filtering needed:

- `catalog.data.gov` / `catalog.data.gov/dataset` — US federal catalog
- `data.europa.eu` — EU open data portal
- `huggingface.co/datasets` (no `?search=`) — HF dataset index root
- `kaggle.com/datasets` (no `?search=`) — Kaggle root
- `data.world` — community portal
- `ourworldindata.org/charts` — OWID charts index
- `data.cdc.gov/browse` — CDC open data
- `zenodo.org/communities` — Zenodo community list

If `huggingface.co/datasets?search=X` shows up — that's NOT in this list,
it's an interior list page. Use render-then-extract instead.

## Examples of INTERIOR LIST PAGES (use render-then-extract)

- `huggingface.co/datasets?search=sentiment+analysis`
- `kaggle.com/datasets?search=covid`
- `search.jd.com/Search?keyword=laptop`
- `s.taobao.com/search?q=phone`
- `www.zhihu.com/search?q=ML&type=content`
- `hotels.ctrip.com/hotels/list?city=Beijing`
- `catalog.data.gov/dataset?tags=transportation` (filter applied!)
- `arxiv.org/list/cs.LG/2025` (mid-level date-bucketed list)

## Storage note (standalone)

In standalone mode there is no `propose_skill` / domain-skill store. When you
learn a new per-domain URL pattern during a run, just record it as a line in
your run's `state.json` notes or a plain `memory.md` you keep in the workspace,
and cite the page you observed it on (the page you fetched) as evidence — the
same discipline, minus the MCP tool.
