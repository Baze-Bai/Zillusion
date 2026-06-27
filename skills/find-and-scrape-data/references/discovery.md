# Discover Phase — Finding Data Sources (Standalone)

This is the methodology for the **DISCOVER** phase: turn a user's data need into a vetted list of candidate sources, each a structured record. It is distilled from the Zillusion backend discovery agent, which ran a LangGraph chain (`route → discover → coarse_filter → portal_detect → expand_portals → classify → dedupe`) behind a 30-tool MCP server. You have none of that. You have `WebSearch`, `WebFetch`, `Bash` (python/curl), `Read`, `Write`, `Edit`, `Glob`, `Grep`, and plain files. The doctrine carries over intact; the mechanics get simpler and a few capabilities degrade — both are spelled out below.

The SKILL.md router owns the 3-phase orchestration, the capability probe, the `goal.md` handoff, the `state.json` ledger, and the **"everything discovered is a HYPOTHESIS not a fact"** doctrine. This file is only the *how* of phase 1.

## Core doctrine (carries over verbatim-ish)

These four rules are why the original worked. They cost nothing to keep and they are where a vanilla agent most often goes wrong.

- **Neutral / no-bias search.** APIs, files, dataset pages, commercial listing pages, consumer search/category pages, forums, encyclopedias, government portals are *all equally valid* by default. Do **not** auto-inject qualifiers like `API`, `developer`, `open platform`, `dataset` into your queries unless the user's literal need fits them. The original prompt's words: *"Keep search_web queries NEUTRAL — avoid auto-injecting qualifiers … unless they genuinely fit the user's literal need."* A query for "北京酒店数据" is not a query for "北京酒店 API".
- **Evidence-anchoring.** Every claim about a source — its fields, its auth, its license, that it even *has* data — must be tied to a page you actually fetched. When you assert something, cite the `WebFetch` you ran or the file you saved (`workspace/fetched/<slug>.md`). A source you only saw in a search snippet is a *lead*, not a finding; mark it so. Quote concrete numbers ("list page shows 4,550 hotels") rather than vibes.
- **Everything is a hypothesis.** The SKILL.md doctrine in full. Here it means: `source_type`, `fields_present`, `auth_type`, `access_level` are your *best current guess*, to be overturned by the explore phase or by a deeper fetch. Never launder a guess into a fact in the record — flag low-confidence fields.
- **Emit-as-you-go.** The original committed each source to `sources.jsonl` *the moment it was accepted*, not in one final dump, because discovery runs long and can be interrupted mid-way (rate limits, a crash, the user steering a pivot). Do the same: **append each accepted source to `workspace/sources.jsonl` as you go** (one JSON object per line, via `Write` to create then `Edit`/append, or `Bash` `>>`). Holding everything in context to the end means an interrupted run loses everything and a long run re-narrates structure from fuzzy memory. The flat file *is* the deliverable; your final message is a convenience summary of it.

> Original mechanism → vanilla equivalent: the backend's `commit_source` / `commit_portal_tree` tools (with tool-side dedup + tombstones + `PreToolUse` append-only enforcement) do not exist here. You append to plain `.jsonl` yourself. There is **no hook stopping you from rewriting history** in that file — self-discipline replaces the guardrail. Append, don't rewrite; if you retract a source, append a `{"_tombstone": true, "url": "...", "reason": "..."}` line rather than deleting the original.

## The DISCOVER procedure

### 1. Intent before IO — write the task note first

Before any search or fetch, write what you understood to `workspace/task_description.md` (the SKILL.md `goal.md` handoff explains the downstream contract; here the rule is simply *intent before IO*). The original made this a hard gate: every network tool refused to run until the file existed. Keep that discipline by hand. The note pins your interpretation so the user (if watching) can correct you cheaply, and so *you* can re-read it when a long run drifts. Use these exact H2 headings (downstream parsing relies on them); write the prose in the **user's language**, headings/URLs/field-names in English:

```markdown
## Goal — what the user wants
One paragraph: which records / fields / format, what for, what "done" looks like.

## Constraints
- Geographic / Temporal / License / Format (only if user named one) / Required fields (min schema)

## Discovery Strategy — how you will find these
- Publishers to target first (name them), 3-5 concrete keyword groups,
  which registries/portals are worth hitting, what signals = "good enough",
  what you will explicitly NOT do and why.

## Good enough — when you will stop
Specific: e.g. "10-15 sources spanning >=3 kinds" — not "until it feels enough".

## Open questions / assumptions
- Things you GUESSED about ambiguous intent (the user's cheap correction point).
```

### 2. Batch a small number of high-yield, neutral searches

`WebSearch` is **rate-limited and slower than the original SearXNG fan-out** — you cannot fire 16 narrow queries cheaply. Issue **3-5 broad, neutral queries** that each cover a sub-question, then *read the results* before deciding what to narrow. Apply these per-query directives (the backend computed them as a runtime addendum from the literal query):

| Signal in the user's query | Directive |
|---|---|
| An explicit URL is named | **`WebFetch` that URL FIRST**, before any broad search. Treat named URLs as primary targets; search is supplementary. |
| Scrape verbs (`抓取`/`爬取`/`爬虫`/`采集`/`scrape`/`crawl`/`extract from website`) | Do **not** bias toward APIs/datasets. Consumer listing pages, search-result pages, login-walled SPAs are **equally valid `embedded` candidates**. Lean into page extraction, not API hunting. |
| No format specified | Fully neutral discovery. Don't pre-filter by type. Keep query text plain — no `API`/`developer`/`dataset` qualifiers. |
| A format *is* named (e.g. "JSON API") | Then it's a real constraint — bias toward it, and say so in the task note's "won't do". |

### 3. For academic / dataset / government intents, query free registry APIs directly

This is the **infra-free replacement** for the backend's `query_registry` worker-tag tool. Those registries are public, keyless (or nearly), and return clean structured JSON — far higher signal than scraping search results. Run them with `Bash` (`curl` or a `httpx` one-liner) and save the JSON to `workspace/`. Real endpoints:

```bash
# OpenAlex — scholarly works / venues (no key)
curl -s "https://api.openalex.org/works?search=urban%20air%20quality&per-page=25" > workspace/reg_openalex.json

# Semantic Scholar Graph API (no key for low volume; add x-api-key header if you have one)
curl -s "https://api.semanticscholar.org/graph/v1/paper/search?query=air+quality+sensors&fields=paperId,title,url,abstract,year&limit=25" > workspace/reg_s2.json

# HuggingFace datasets (no key)
curl -s "https://huggingface.co/api/datasets?search=air%20quality&limit=25&full=true" > workspace/reg_hf.json

# CKAN package_search — works on data.gov and ANY CKAN portal (no key)
curl -s "https://catalog.data.gov/api/3/action/package_search?q=air+quality&rows=25" > workspace/reg_ckan.json
```

A python `httpx` equivalent (use if `curl` is absent or you want to post-process inline):

```python
import httpx, json
r = httpx.get("https://api.openalex.org/works",
              params={"search": "urban air quality", "per-page": 25}, timeout=30)
json.dump(r.json(), open("workspace/reg_openalex.json", "w"))
```

`Read` the saved JSON and pull `results[].id/title/...` (OpenAlex), `data[].paperId/url` (S2), the dataset `id`/`cardData` (HF), `result.results[].name + resources[].url` (CKAN — note CKAN's `resources` give you direct **file** download URLs, a `["embedded","file"]` source). Other CKAN portals work by swapping the host (`data.gov.uk`, `data.gov.au`, EU/city portals).

### 4. Fetch the promising candidates

`WebFetch` each lead worth verifying (this replaces `fetch_page` / `probe_url`). Save the result to `workspace/fetched/<slug>.md` so a later step — or the explore phase — can re-read it without a second network round-trip, and so your evidence citations point at a real file. A quick liveness/HEAD check (the old `probe_url`) is just `curl -sI <url>` in `Bash` when you only need status/content-type, not the body. For dynamic pages where `WebFetch` returns a near-empty shell, note that and defer to the explore phase rather than guessing fields.

### 5. Classify each candidate: `api` | `file` | `embedded`

A source can be **multiple** categories — list all that genuinely apply. The canonical example: a dataset landing page that *also* exposes a "Download CSV" button is `["embedded", "file"]`; downstream each category becomes its own workflow.

| Type | Definition |
|---|---|
| `api` | A REST/GraphQL/JSON endpoint **or its documentation page**. |
| `file` | A directly-downloadable structured file (CSV/JSON/Parquet/XLSX/XML/ZIP). |
| `embedded` | A web page whose user-facing content **is** the data: dataset landing page, product/item detail, listing/search/category pages, forum threads serving as data. May need rendering or login and still be valid `embedded`. |

For the **portal-vs-leaf decision** — is this candidate a single leaf source, or a list/hub page that should be expanded into a tree of detail children? — **see `references/portal-detection.md`**. Do not re-derive that judgment here. That file also covers how, lacking `firecrawl_map` / `crawl_list_tree`, you map a portal's URL skeleton by hand (fetch the listing, extract links, cluster by path shape) and when to sample children vs. defer the whole expansion to the explore phase.

### 6. Append accepted sources, applying tree-wins dedup

Append each accepted source as one line to `workspace/sources.jsonl` (schema below). Apply the original's **two dedup rules** yourself, since no tool enforces them:

- **Intra-list:** canonicalize URLs (lowercase host, strip `utm_*`/fragment, drop trailing slash, http→https) and never emit the same canonical URL twice.
- **Tree wins:** if a URL lives *inside* a committed portal tree (it's a child in some `portal_trees[]` entry), it must **not** also appear as a flat source. The tree already represents it; a duplicate flat source double-counts. When you commit a portal tree that contains a URL you earlier emitted flat, append a tombstone line for the flat one.

### 7. For each source you'll scrape, write its `goal.md` — the handoff to Phase 2/3

`task_description.md` (step 1) was your **run-level** working note — one per run, your interpretation of the whole request. It is NOT the handoff contract. For each source the user picks to actually scrape, write a **per-source** `workspace/<site_id>/goal.md`. *This* is the spine the rest of the pipeline reads: Phase 2 (explore) treats it as its starting hypothesis and updates it; Phase 3 (validate) checks the produced data against its `## Required fields`. A source with no `goal.md` is not ready to explore.

Use exactly these headings — Phase 3's field-coverage check parses required fields **only** from a heading literally named `## Required fields` (or `## Output fields` / `## Output schema`), so they must NOT be buried under `## Constraints` or any other heading:

```markdown
# goal: <site_id>

## Goal
<one paragraph: what records the user wants from THIS source, what "done" looks like>

## Source
- url: <the seed URL for this source>
- source_type: <api | file | embedded — the discovery HYPOTHESIS, to be confirmed in explore>

## Required fields
- `field_a` — <what it means>
- `field_b` — <what it means>
<!-- list every field the output records MUST carry, name in backticks, one per
     bullet. For weak-schema data the minimum is `source_url` + `content`/`file_ref`. -->

## Notes / hypotheses for explore
- <auth? pagination shape? JS-rendered? portal to expand? anything Phase 2 should probe first>
```

Keep `goal.md` honest as reality diverges from the hypothesis — it is read, not archived.

## Output schema

Two record kinds. A flat source goes one-per-line in `sources.jsonl`; portal trees go one-per-line in `workspace/portal_trees.jsonl`. Your final summary message can also bundle them as one JSON object with `sources`, `portal_trees`, and `exploration_notes` keys.

**Flat source** (required: `url`, `name`, `source_type`, `description`, `discovery_method`):

```jsonc
{
  "url": "https://...",
  "name": "...",
  "source_type": "embedded",            // single string OR list e.g. ["embedded","file"]
  "description": "<= 280 chars",
  "discovery_method": "web_search",     // web_search | registry | llm_prior | warm_start | portal_expansion
  "provider": "...", "domain": "...",
  "access_level": "unknown",            // open | free_reg | api_key_free | api_key_paid | oauth | paywall | unknown
  "tags": [], "data_format": ["json"],
  "geographic_coverage": ["..."], "temporal_coverage": "...", "update_frequency": "...", "license": "...",

  // ── emit these top-level fields ONLY when source_type includes "api" ──
  "api_endpoint": "https://api.../v1/...", "api_method": "GET",
  "auth_type": "api_key",               // api_key | oauth | hmac | none | unknown
  "auth_location": "header",            // header | query | body
  "auth_param_name": "X-Api-Key",
  "signup_url": "https://...",          // MANDATORY for any non-open API — where the USER registers for a key
  "signup_instructions": "one-line how-to-register",
  "docs_url": "https://...", "openapi_spec_url": "https://...", "has_sdk": false,

  // ── when source_type includes "file" ──
  "download_url": "https://.../data.csv", "file_format": "csv",

  // ── when source_type includes "embedded" ──
  "extraction_method": "...", "data_shape": "table|list|cards",
  "fields_present": ["..."], "extraction_difficulty": "low|medium|high",

  "metadata": { "evidence": "why this is a data source — cite the fetched file/url", "fields_present": ["..."] }
}
```

`signup_url` is **as mandatory as the endpoint** for any keyed API: without it the user cannot obtain a key and the source is useless to them. If the docs page didn't carry it, run `WebSearch("<provider> API key signup")`, read the result, and fill it before accepting. If you still can't find a precise page after searching, emit what you have (provider + homepage `url` + `auth_type`) so the user at least lands at the right developer portal — don't drop the source.

**Portal tree** (`portal_trees.jsonl`; see `references/portal-detection.md` for when to produce one):

```jsonc
{
  "root": {
    "url": "...",
    "page_type": "list",                // list | detail | category | pagination | hub
    "title": "...", "depth": 0,
    "fields_available": ["..."],
    "record_count": 200, "is_sampled": true,
    "children": [ /* DataPageNode: same shape, each requires url, page_type, depth */ ]
  },
  "total_detail_pages": 137,
  "sampled_detail_pages": 2,
  "field_progression": { "list_page": ["name","price"], "detail_page": ["name","price","address","phone"] },
  "tree_summary": "what this portal is, how many records, what fields appear where (quote numbers)"
}
```

`page_type` meanings: **list** = many homogeneous items (usually the root); **detail** = one record (a list child); **category** = navigation grouping with no record-level listing; **pagination** = a "next page" continuation (usually collapse into the parent); **hub** = a landing page mixing nav and highlights without listing records. A crawl whose root has **0 children is not a tree** — emit it as a flat source, not a single-node tree wrapping emptiness.

Also emit a free-form `exploration_notes` string: what you tried, what you skipped and why, where you're least confident. This is the hypothesis-honesty record for the explore phase.

## Tool mapping: backend → vanilla CC

| Backend tool | Vanilla CC technique |
|---|---|
| `search_web` | `WebSearch` (rate-limited — batch 3-5 broad neutral queries, not 16 narrow ones). |
| `fetch_page` / `probe_url` | `WebFetch` for the body (save to `workspace/fetched/`); `curl -sI` in `Bash` for a HEAD/status-only probe. |
| `query_registry` | **Direct registry API calls** via `Bash` curl/httpx — OpenAlex, Semantic Scholar, HuggingFace datasets, CKAN `package_search` (step 3). High-signal, keyless, infra-free. |
| `query_api_directory` | **LOST.** There is no portable 23k-API semantic index (the backend's `unified.sqlite` + bge-m3/BM25). Replacement: `WebSearch` over public API directories (`publicapis.io` / `github.com/public-apis/public-apis`, `apis.guru` and its `https://api.apis.guru/v2/list.json` machine-readable dump, RapidAPI hub) **plus** the registry APIs above. Expect lower recall on obscure/long-tail APIs — say so in `exploration_notes`. |
| `firecrawl_map` / `crawl_list_tree` | No firecrawl, no SearXNG, no guaranteed playwright. Map portals and build trees by hand — **see `references/portal-detection.md`** and the explore phase. |
| `cluster_urls_by_skeleton` / `sample_cluster` | Pure-local URL grouping — re-implement in a throwaway `Bash`/python snippet (split paths, replace numeric/UUID/slug segments with `{id}`, group). Covered in `references/portal-detection.md`. |
| `commit_source` / `commit_portal_tree` / `check_url_committed_status` / `remove_committed_source` | Append a line to `workspace/sources.jsonl` / `portal_trees.jsonl`; `Grep` the file to check if a URL is already committed; append a `{"_tombstone": true, ...}` line to retract. You enforce dedup/tree-wins manually. |
| `send_user_message` | Just write to the user in your normal response text. |
| `lookup_skill` / `propose_skill` / `memory_append` | No cross-run skill/memory library. Record durable lessons in `exploration_notes` or a `workspace/notes.md` for this run only. |

## What's weaker standalone (be honest about it)

- **No 23k-API semantic index.** Long-tail / cross-language API recall drops to whatever `WebSearch` + the public directories surface. Flag this gap in `exploration_notes` when the user wanted an obscure API.
- **No firecrawl/SearXNG/managed crawl.** Portal mapping and tree-building are hand-rolled and shallower; `WebFetch` can't render JS, so dynamic listings may look empty — defer those to the explore phase rather than asserting "no data".
- **No PreToolUse append-only enforcement.** Nothing stops you (or a bug) from rewriting `sources.jsonl`. Treat it as append-only by discipline; tombstone instead of delete.
- **`WebSearch` rate limits** make the broad fan-out of the original impractical — fewer, smarter queries, and read before you narrow.
- **No persistent skill/memory across runs.** Each run starts cold; lessons live only in this run's notes.

When a capability is missing, the right move is to **lower the confidence on the affected fields and mark them as hypotheses** — not to fabricate a value to fill the schema. That is the whole point of the hypothesis doctrine.
