# Discovery architecture — how the web app finds data sources

The self-hosted web app finds candidate data sources through **two parallel
channels**, then **fetches** the promising ones to confirm and classify them.
By default it needs **no search API keys** — the only required search
infrastructure (SearXNG) is self-hosted and bundled in `docker-compose.yml`.

```
                ┌──────────────────────────────────────────────┐
  user query →  │  A. Registry direct-query    B. Web search    │ → candidate
                │     (source registries'         (search_web)   │   sources
                │      APIs, structured)          via SearXNG     │
                └───────────────────────┬──────────────────────┘
                                        │ fetch_page
                                        ▼
                         firecrawl → jina → httpx   (read + classify)
```

## Channel A — Registry direct-query (structured)

The pipeline queries source registries directly through their APIs
(`query_registry` + the adapters in `backend/src/adapters/`). These are
structured source catalogs, not web search:

| Domain | Registries |
|--------|-----------|
| Academic | OpenAlex, Semantic Scholar |
| Datasets | Hugging Face, Kaggle |
| Government / open data | CKAN portals (data.gov, data.gov.uk, EU Open Data Portal, and ~20 national / regional instances) |
| Code | GitHub |
| Geo / MCP-backed | additional adapters under `adapters/geo`, `adapters/mcp` |

Most need no key (OpenAlex / CKAN / Hugging Face are open); a few accept an
optional token to raise rate limits — see `backend/.env.example`, section 12
("ADAPTER DEFAULTS & CREDENTIALS").

## Channel B — Web search (`search_web`)

For sources not in the registries, the agentic discovery node calls `search_web`,
which runs a provider fallback chain (`backend/src/tools/search/`):

```
preferred order:  searxng → brave → tavily → exa
```

- **SearXNG is the primary, default engine** — self-hosted, free, no API key,
  shipped in `docker-compose.yml`. With no other keys set, it is the only
  web-search provider used.
- **Brave, Tavily, Exa** are optional commercial fallbacks, used only if you set
  their keys (`SEARCH_BRAVE_API_KEY`, `SEARCH_TAVILY_API_KEY`,
  `SEARCH_EXA_API_KEY`). Exa is neural / semantic search.

### What SearXNG aggregates

SearXNG is a meta-search engine. The bundled `searxng/settings.yml` enables:

| Category | Upstream engines |
|----------|------------------|
| General web | Google, Bing, DuckDuckGo, Brave |
| Knowledge / data | Wikipedia, GitHub, arXiv, Semantic Scholar |

(Google Scholar is configured but disabled.) Results come back as JSON; the local
instance runs with the rate limiter off. Search categories used:
`general, science, files` (`SEARCH_SEARXNG_CATEGORIES`).

So with **zero commercial keys**, the engines actually reaching the internet are
**Google / Bing / DuckDuckGo / Brave / Wikipedia / GitHub / arXiv / Semantic
Scholar**, all aggregated through your self-hosted SearXNG.

## Fetching (after search)

`search_web` only returns URLs. Promising candidates are read by `fetch_page`,
which runs a *separate* fallback chain:

```
firecrawl → jina → httpx
```

The lean compose does **not** bundle Firecrawl (bring-your-own: cloud key via
`SEARCH_FIRECRAWL_API_KEY`, or self-host and point
`SEARCH_FIRECRAWL_SELF_HOSTED_URL` at it). When Firecrawl is absent the chain
degrades to jina / httpx, which read plain HTML and server-rendered pages fine.
`SEARCH_JINA_API_KEY` configures the jina reader — this is fetching, not search.

## Configuration summary (`backend/.env`)

| Variable | Purpose | Default |
|----------|---------|---------|
| `SEARCH_SEARXNG_URL` | self-hosted SearXNG endpoint | `http://searxng:8080` (compose) |
| `SEARCH_SEARXNG_CATEGORIES` | engine categories | `general,science,files` |
| `SEARCH_BRAVE_API_KEY` / `SEARCH_TAVILY_API_KEY` / `SEARCH_EXA_API_KEY` | optional commercial web search | empty (skipped) |
| `SEARCH_FIRECRAWL_USE_SELF_HOSTED` / `SEARCH_FIRECRAWL_SELF_HOSTED_URL` / `SEARCH_FIRECRAWL_API_KEY` | page fetch via Firecrawl | bring-your-own |
| `SEARCH_JINA_API_KEY` | jina reader (fetch fallback) | empty |

**Bottom line:** discovery runs out of the box with **no search keys** — registry
APIs plus a self-hosted SearXNG meta-search, with optional commercial providers
if you add keys.
