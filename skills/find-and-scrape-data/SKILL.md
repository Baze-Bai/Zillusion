---
name: find-and-scrape-data
description: Find data sources for a question, explore a chosen source, then write, validate, and run a scraper for it — the whole discover-to-explore-to-build pipeline using only vanilla Claude Code tools (no backend, MCP server, or API keys needed to run). Use this whenever the user wants to discover where to get data on a topic, find which dataset or API has some data, or scrape/crawl a specific site and build a working, runnable scraper or crawler. Triggers include "find/discover data sources for X", "where can I get data about Y", "what dataset has Z", and "scrape this site and build me a scraper". The deliverable is EITHER a vetted list of data sources OR a runnable scraper for a chosen source. Do NOT use it for a single factual web lookup (just search and answer), for analyzing or charting data the user already has, or for editing, fixing, or debugging a scraper that already exists.
compatibility: Vanilla Claude Code (WebSearch, WebFetch, Bash, Read/Write/Edit, Grep). Optional python httpx/playwright for live probing and JS-rendered sites. Runs with no backend, no Firecrawl/SearXNG, no MCP server, and no API keys.
metadata:
  version: 0.2.0
  source: Zillusion data pipeline (discover -> explore -> build)
---

# find-and-scrape-data

Turn a data need into either **a vetted list of data sources** or **a working,
validated scraper** — using only vanilla Claude Code tools (`WebSearch`,
`WebFetch`, `Bash` with python/httpx/playwright, `Read`/`Write`/`Edit`,
`Glob`/`Grep`). This skill is a distillation of the Zillusion pipeline; it
needs **no backend, no Firecrawl, no SearXNG, no MCP server** to run.

The work happens in three phases — **DISCOVER → EXPLORE+ROUTE → BUILD+VALIDATE+RUN**.
This file is the **router**: it sets up the run, names the cross-phase
contracts and invariants, and tells you which one reference to read for the
phase you're in. **Read only the reference for your current phase** — loading
all of them at once wastes context. Do not inline reference content here.

## 0. Start of run — set up once

1. **Pick a workspace dir** for this run (default `./fsd-<slug>/`, or a dir the
   user names). Everything below lives under it.
2. **Capability probe (run ONCE, record the result in `state.json`).** It
   decides two things: how you fetch pages, and whether you can hand off to the
   real harness.

   ```bash
   # browser available?
   python -c "import playwright" 2>/dev/null && echo "playwright: yes" || echo "playwright: no"
   # http client (the skill is httpx-first)?
   python -c "import httpx" 2>/dev/null && echo "httpx: yes" || echo "httpx: no"
   # real harness present as a sibling? (lets you optionally delegate phases 2-3)
   # Test a TRACKED path: .venv/ is gitignored, so probing for it reports
   # "no" on every fresh clone and this branch would never be reachable.
   test -f ../harness/runtime/cli.py && echo "harness: yes" || echo "harness: no"
   ```

   - **`playwright: no`** → fetch with `httpx`/`WebFetch` only; reach for a
     browser only when a page is genuinely JS-rendered, and only after
     `pip install playwright && playwright install chromium` (ask the user if
     that install is unwelcome). Many "embedded" pages are server-rendered or
     carry their data in an embedded JSON blob — **httpx-first, browser-on-demand**.
   - **`httpx: no`** → `python -m pip install httpx` (tiny, pure-python), or fall
     back to `curl` / `urllib` / `WebFetch`. The skill is httpx-first by doctrine —
     confirm it up front rather than discovering the gap inside your first probe.
   - **`harness: yes`** → after Phase 1 you MAY delegate the chosen sources to
     the real harness instead of doing Phases 2-3 by hand (it has a persistent
     crawl agent, append-only enforcement, and an embedded-browser takeover you
     don't have standalone). Offer this to the user; otherwise proceed standalone.
3. **Create `state.json`** — the run ledger, so a resumed/multi-turn session
   knows where it is (there is no orchestrator tracking it for you):

   ```jsonc
   {
     "query": "<the user's data need>",
     "phase": "discover",
     "capabilities": {"playwright": false, "httpx": true, "harness": false},
     "sources": [],          // accepted sources, appended as you go
     "chosen": [],           // sources the user/you picked to scrape
     "routes": {}            // per chosen-source: inline|deterministic|agentic|infeasible
   }
   ```

## The two contracts that bind the phases

- **`goal.md` (one per chosen source) is THE handoff contract.** Phase 1 writes
  it (intent + hypothesized fields + a `## Required fields` list). Phase 2 reads
  it as its starting hypothesis and updates it. Phase 3 validates the produced
  data *against its `## Required fields`*. If a source has no `goal.md`, it is
  not ready to explore. This file is the spine — keep it honest as reality
  diverges from the hypothesis.
- **`state.json` is the run ledger** (above). Update `phase`, `sources`,
  `chosen`, `routes` as you go.

## Cross-phase invariants (the glue — details live in the references)

- **Everything discovered is a HYPOTHESIS, not a fact.** A WebSearch snippet, a
  discovered `source_type`, a guessed field — all must be confirmed by probing.
  Discovered type wrong? Switch within the pivot rules. Field not extractable?
  Drop/add it.
- **Evidence-anchoring.** Every claim ties to a page you actually fetched; cite
  the fetched file/URL. Don't assert structure you haven't seen.
- **Route before artifact.** Decide the terminal route (inline / deterministic /
  agentic / infeasible) BEFORE writing `workflow.py`. Building first is the #1
  failure mode.
- **Emit as you go.** Append accepted sources / records to plain files
  (`sources.jsonl`, incremental `output.json`) as you find them — discovery and
  crawls are long and killable; don't hold everything in memory to the end.
- **Weak schema is legal.** A record can be one data unit (a document, a media
  file) — minimum is `source_url` + a content carrier (inline `content` or a
  `file_ref` to a saved file). Don't force fake fields onto a single document.
- **Credentials never leave python.** Load keys inside the script; never put a
  key in a CLI arg, a print, or a manifest. Never self-register for keys —
  surface the signup URL to the user.
- **Honest verdicts.** Hard-verifiable things gate (parses, is-a-list, required
  fields present, format parses, no secret leaked). Completeness is NOT
  hard-verifiable in one pass — never `FAIL` on a "feels incomplete" hunch; mark
  it `INCONCLUSIVE` and log a follow-up.

## The three phases

### Phase 1 — DISCOVER  → read `references/discovery.md`
Given the query, find candidate data sources. Neutral WebSearch + direct calls
to free registry APIs (OpenAlex / Semantic Scholar / HuggingFace / CKAN) +
`WebFetch` of candidates; classify each into `api | file | embedded` (may be
multiple). Use `references/portal-detection.md` to decide if an embedded URL is
a portal to expand. **Produces:** `sources.jsonl` (ranked) and a `goal.md` for
each source the user chooses to scrape. Update `state.json.phase = "explore"`.

### Phase 2 — EXPLORE + ROUTE  → read `references/explore-and-route.md`
For a chosen source, run the hypothesis loop (PICK_NEXT → PROBE → UPDATE) with
**httpx-first, browser-on-demand** probing; for APIs use the browserless probe
ladder with credential safety. Then **decide the route** (inline / deterministic
/ agentic / infeasible) and record it in `state.json.routes`. **Produces:**
`hypotheses.yaml`, probe samples on disk, a declared route.

### Phase 3 — BUILD + VALIDATE + RUN  → read `references/build-validate-run.md`
For the `deterministic` route, write `workflow.py` (honoring `CRAWL_MODE`,
atomic flush, heartbeat) + exactly one manifest (`selectors.yaml` /
`download_manifest.yaml` / `api_manifest.yaml`). Validate the sample output
against `goal.md`'s required fields and emit a final regex-parseable verdict
line `[PASS|FAIL|INCONCLUSIVE]`. On `PASS`, run at full scope into
`runs/<run_id>/output.json`. (`inline` harvests now; `agentic` writes a
`crawl_brief.md` and hands back; `infeasible` stops.)

## Where to enter
- Argument is a **topic / question** → start at Phase 1.
- Argument is a **URL** → still write a `goal.md`, but Phase 1 is short:
  `WebFetch` that URL first (it's the primary candidate), then go to Phase 2.
- User asks **"is the scraper ready?"** for an existing workspace → go to
  Phase 3 validation.

## Anti-patterns
- Reading every reference up front (read only the current phase's).
- Calling tools that don't exist here (`firecrawl_map`, `crawl_list_tree`,
  `commit_source`, `workspace_append_log`, `browser_player`, …) — they were
  backend/harness MCP tools; use the vanilla equivalents the references give.
- Writing `workflow.py` before declaring a route.
- Self-registering for an API key, or pasting a key into a command/manifest.
- Dumping a large fetched body into the conversation — save it to disk, then
  `Read`/`Grep` the file.

## What's weaker standalone (be upfront with the user)
No 23k-API semantic index (replaced by WebSearch + the registry APIs); no
persistent agentic crawler (the `agentic` route hands back); no append-only or
secret-leak enforcement hooks (you self-discipline; Phase 3 still does a secret
scan); a browser may need installing. Everything else maps cleanly to vanilla
Claude Code.
