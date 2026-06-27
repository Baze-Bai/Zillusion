# Explore a source, then route it

This is **Phase 2** of `find-and-scrape-data`. Phase 1 (discover) handed you a `goal.md` naming one seed URL + what data the user wants. Your job: turn a few unverified guesses about that source into confirmed facts, then **declare exactly one of four routes**. You do NOT build the scraper here — that's Phase 3 (`build-validate-run.md`). The single worst mistake in this phase is jumping straight to writing `workflow.py`; decide the route *first*.

Everything you learn is a **HYPOTHESIS until a probe confirms it** (the SKILL.md doctrine). The seed URL, the "likely workflow type" Phase 1 guessed, the field list — all priors. Verify each non-trivial one against the live source before you lean on it.

This phase ends not with a `[DONE]` marker but by **writing a declared route** into `state.json` (the run-ledger SKILL.md owns) and into `goal.md`. A session that stops without a declared route is unfinished.

## The hypothesis loop

```
PICK_NEXT  -> PROBE (one bounded check) -> UPDATE (confirmed|refuted|partial|blocked)
   ^                                              |
   +----------------- loop while unverified ------+
                                                  |
                  all key hypotheses resolved -> ROUTE DECISION -> record route
```

- **PICK_NEXT** — take the highest-`priority` hypothesis still `unverified`. None left → go to ROUTE DECISION. Pick the one that most *gates the route*: "is the data JS-rendered?" and "how big is the full set?" decide everything downstream, so probe those before cosmetic field questions.
- **PROBE** — run ONE bounded check that yes/no's that single hypothesis (patterns below). One probe, one question.
- **UPDATE** — edit `hypotheses.yaml` to set the new `status` and write the evidence into `result`/`notes`. Append a one-line note to `exploration_log.md` (a plain file you create with Write, then grow with Edit) so a later iteration can see what you already tried.

> **Recon: read the claimed total before you commit.** Most listing/search pages and their JSON backends expose an inventory size — `totalCount` / `total` / `numFound` / a "X results" string / a last-page number. Find it early and record it as a hypothesis: it is your **ceiling** and your **cap-detector**. If a probe yields 240 records but the page claims 7,129, you have a *slice*, not the set — a route- and coverage-gating fact (usually per-query caps + the need to tile, see Coverage caps below). "I got some records" ≠ "I got the records" until you've compared your harvest against the claimed count. This also gates ROUTE test #1 (inline needs a genuinely tiny *full* set) and the agentic hard-anchor.

### hypotheses.yaml

In the original harness this file was written only through an `hypothesis_set_status` tool behind a PreToolUse append-only hook. **You have neither.** Standalone, it is just a plain YAML file in the workspace that you create with Write and mutate with Edit directly. The discipline (don't silently delete a refuted hypothesis — flip its status and keep the counter-example) is now on *you*, not enforced by tooling.

```yaml
- id: h1
  claim: "Listing data is embedded in the server-rendered HTML, no JS needed"
  source: "Phase 1 seed.json said page_type=embedded"
  status: unverified        # unverified | confirmed | refuted | partial | blocked
  priority: high            # low | medium | high
  result: null              # one-line outcome once probed, else null
  notes: null               # free-form; counter-example if refuted
  wall_type: none           # login | login_modal | captcha | challenge | none
                            # meaningful only when status == blocked
```

Seed it with 3-6 hypotheses up front (Is it JS-rendered? Is there an internal/public API? What is the record container? How is it paginated? How big is the full set? Any auth wall?), then work them down by priority.

## PROBE patterns — httpx-FIRST, browser only on demand

This ordering is not a style preference. It is **cheaper**, and — critically — **playwright may not be installed**. The SKILL.md capability-probe already ran `python -c "import playwright"` and recorded the answer in `state.json`; consult it instead of assuming. So always try the browserless path first.

**(a) Try httpx / WebFetch first.** A surprising fraction of "needs a browser" pages are server-rendered or carry the data in an embedded blob. Before launching anything, fetch the raw HTML and grep it:

- `<script type="application/ld+json">` — structured data, often the whole record list.
- `__NEXT_DATA__` / `__NUXT__` / `window.__INITIAL_STATE__` — the SPA's hydration JSON. Parse it and you skip the browser entirely.
- a plain `<table>`, an RSS/Atom feed, a sitemap, or a `?format=json` variant of the same URL.
- a Network-visible `/api/...` path referenced in the inline JS.

> **When you slice an embedded blob into records, anchor on a STABLE id + verify consistency.** Embedded JSON is often a flat stream you cut into per-record windows by regex. Anchoring those windows on a field whose position relative to the record boundary is *unpredictable* (a `detail_url`, a price — any may render before OR after the record's own id) causes an off-by-one: record N's value gets paired with record N+1. Anchor each window on the one key that reliably marks a boundary — a stable per-record **id** — then **cross-check**: the window's id must equal the id embedded elsewhere in the same record (e.g. the numeric id inside its detail URL); skip/flag records whose ids disagree. (Real run: switching to id-anchor + URL-id check took alignment from broken to 20/20.) Same discipline applies to regex over repeated DOM, not just JSON.

```python
# illustrative — you write this at runtime, save the body WHOLE to disk, then Read/Grep it
import httpx, pathlib
r = httpx.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True)
pathlib.Path("workspace/samples/seed.html").write_bytes(r.content)   # never dump into context
print(r.status_code, len(r.content), r.headers.get("content-type"))
```

Then `Grep` the saved file for `__NEXT_DATA__` / `ld+json` / the field values you expect. If you find them, the source is *extraction* (or even *inline*) and you never needed a browser — record that as a confirmed hypothesis and downgrade nothing.

> **JS-rendered, decided by a count not a vibe:** a page is JS-rendered *iff* the record VALUES you expect are absent from BOTH the raw httpx HTML AND any embedded blob (`__NEXT_DATA__` / `__NUXT__` / `ld+json`). Count actual records found, not whether tags exist — empty container `<div>`s plus a populated hydration blob is NOT JS-rendered (parse the blob, stay browserless).

**(b) Only when the data is genuinely JS-rendered**, drive Playwright via `Bash` python. The original harness used a CDP-first MCP surface (`browser_player`, `browser_cdp_send`, `browser_goto`, `browser_snapshot`); none of those exist for you. The vanilla equivalent is a throwaway Playwright script:

```python
# illustrative — minimal async Playwright probe (you write + run this via Bash)
import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        xhr = []                                  # sniff hidden API/GraphQL traffic
        page.on("response", lambda resp: xhr.append((resp.url, resp.status))
                if "/api/" in resp.url or "graphql" in resp.url else None)
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_selector(".result", timeout=8000)
        html = await page.content()
        rows = await page.eval_on_selector_all(
            ".result", "els => els.map(e => e.querySelector('.title')?.innerText)")
        await browser.close()
        # write html WHOLE to workspace/samples/, print only small summaries
        print(json.dumps({"rows_seen": len(rows), "xhr": xhr[:20]}, ensure_ascii=False))

asyncio.run(main())
```

The `page.on("response", ...)` sniffer is the high-value move: if the page fetches `/api/search?q=...` or a GraphQL endpoint, capture that URL and **re-probe it directly with httpx** — an internal JSON API is far more stable than scraping rendered DOM. (That said, treating a page-embedded API as your *route* has a caveat — see Pivot rules.)

**If `import playwright` fails:** the capability-probe already told you. Either, with user consent and if cheap, `pip install playwright && playwright install chromium` via Bash — or fall back to the httpx + embedded-JSON path above and **DOWNGRADE the route's confidence** (note in `goal.md` that you couldn't confirm JS-rendered behavior, so the deterministic workflow may need a browser the runtime lacks).

### Anti-bot ladder — a 200-with-empty-body is a SIGNAL, not "no data"

A listing/search backend can return HTTP **200 with a well-formed envelope carrying zero records** (`{"data":{"list":[]},"totalCount":0}` while the page itself claims thousands). That is rarely "the site has no data" — it is **bot detection answering politely**. Treat a right-shaped-but-empty body as *refuted-by-defense*, not confirmed-empty, and climb one rung at a time, stopping the instant real records appear:

| Rung | Probe | Real result |
| --- | --- | --- |
| 1 | raw `httpx.get` (with a `User-Agent`) | records → done, no browser needed |
| 2 | **headless** Playwright (`launch(headless=True)`) | records → deterministic-with-browser |
| 3 | **headed** Playwright + light stealth | records → headed+stealth is REQUIRED; record it in `goal.md` |

Light stealth = mask the two cheapest automation tells before `goto`:

```python
browser = await pw.chromium.launch(
    headless=False,                                       # headed matters
    args=["--disable-blink-features=AutomationControlled"])
ctx = await browser.new_context(user_agent="Mozilla/5.0 ...")
await ctx.add_init_script(
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
```

Only climb when a lower rung is *provably* empty-by-defense — going headed defeats much production automation, so never preemptively. The rung that first yields data is the **floor `workflow.py` must reproduce** (headed+stealth is coverage-gating — write it into `goal.md` so Phase 3 honors it). If rung 3 still returns empty-shaped 200s, *now* suspect a real wall (below) — not before.

### Pagination boundary (do this before declaring deterministic)

Walk to the last expected page, then **one past the end**. Three termination shapes: an empty result list, a 404/5xx, or a "no more results" string. Workflows that don't terminate cleanly are the #1 post-build regression — confirm which shape applies and record it as the stop condition. Pace the walk: a 0.5–1s inter-page sleep plus the same 429/503 + `Retry-After` backoff the API ladder uses (rung 7) — an unthrottled walk gets you IP-blocked mid-explore.

### Coverage caps and tiling — past the per-query ceiling

Don't conflate two ceilings. The **pagination boundary** (above) is where one query's pages run out. A **result cap** is the backend refusing to paginate past N *total* for any single query — common on OTA / search / marketplace / portal APIs. Detect it with the recon move: your harvested count plateaus far below the claimed total.

Two consequences, both general:
- **SEO / static list pages are a shallow slice, not the set** — often ~20 items/page with no deep URL pagination (`?page=2` 404s or just re-serves page 1). Good for confirming fields/structure, useless for coverage. The full inventory lives behind the **internal data API** the page calls (find it with the `page.on("response", ...)` sniffer above).
- **Beat the cap by TILING + dedup.** Partition the query by a facet the backend honors — district / sub-area / category / price band / date window — so each tile stays under the cap; paginate each tile to its own boundary; **dedup by the stable per-record id** (the same id you anchor extraction on) as you merge. Overlapping tiles are fine — id-dedup makes it idempotent.

```text
for facet in facets:                 # each tile small enough to dodge the cap
    page = 1
    while True:
        rows = api(query, facet=facet, page=page)
        if not rows: break
        for r in rows: seen[r["id"]] = r   # dedup by stable id across tiles
        page += 1
records = list(seen.values())
```

If even exhaustive tiling can't reach the claimed total, that residual gap is an INCONCLUSIVE-grade completeness limit — record it, don't hard-FAIL (completeness isn't hard-verifiable in one pass).

## API probing (browserless)

When the source IS an HTTP API (Phase 1 may have saved an OpenAPI spec during discovery — e.g. `api_spec.json` — if any), probe with `Bash` + httpx only — no browser. Two iron rules carry over verbatim from the harness `api-probe` skill:

1. **Responses land WHOLE on disk** (`workspace/samples/`), never dumped into context. Write the body to a file, then `Read`/`Grep` it selectively (a head, one record, a key path). A 5 MB OpenAPI spec or a paginated list will blow your context if you cat it.
2. **Credentials NEVER leave python.** Load the key *inside* the script; never interpolate it into a shell command (command lines get logged), never `print()` it (mask as `sk-...3f2`), never write it into a manifest / workflow.py / log. The manifest records a *pointer* ("env `API_KEY`, else `credentials.json` walk-up"), never the value.

The exact credential-finder to paste into every probe and into the eventual workflow.py:

```python
import json, os
from pathlib import Path

def _find_credentials() -> dict:
    if os.environ.get("API_KEY"):                       # env wins (for deployments)
        return {"api_key": os.environ["API_KEY"], "extra": {}}
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:         # walk up looking for the file
        cand = parent / "credentials.json"
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    return {}
```

**Probe ladder, cheapest first** — stop as soon as a rung answers your current hypothesis:

| Rung | Check | Yields |
| --- | --- | --- |
| 1 docs | `WebFetch` the documentation/quickstart URL | endpoint paths, auth header name, paging params |
| 2 OpenAPI | if a spec URL exists, httpx it to `samples/` whole, list `paths` keys | the real endpoint surface |
| 3 unauth call | hit the best endpoint with NO auth | 200 = open API; 401/403 = auth required; 404 = wrong path |
| 4 auth call | same call with the key applied (header vs query vs bearer) | the working auth scheme |
| 5 shape probe | find the record list's dot-path (`data.items`) | the `record_path` for the manifest; note parallel-array APIs that need zipping |
| 6 pagination | walk one page past the end | empty / `next:null` / 4xx → the stop condition |
| 7 rate limit | read `X-RateLimit-*` / `Retry-After` off a real response | pacing for workflow.py |

```python
# illustrative rung-4 probe — write, run, then Read the saved sample
import httpx, pathlib
creds = _find_credentials()
headers = {"X-Api-Key": creds.get("api_key", "")} if creds.get("api_key") else {}
r = httpx.get("https://api.example.com/v1/posts?page=1&per_page=5", headers=headers, timeout=30)
pathlib.Path("workspace/samples/ep_posts_p1.json").write_bytes(r.content)   # WHOLE body
print({"status": r.status_code, "bytes": len(r.content),
       "rate": {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}})
```

**Never self-register for keys.** If auth is required and no key is present, mark the hypothesis `blocked` with `wall_type=login`, and tell the user *which* key/plan is missing. Signing yourself up for an API is out of bounds.

## Access walls

Whenever you **can't get the data you came for** — blank page, redirect to a login URL, a "sign in to continue" modal over the content, a CAPTCHA, or a Cloudflare "just a moment" challenge — suspect a wall. Detect it (an httpx 401/403, a login-form selector present, a challenge interstitial in the HTML), then classify into `wall_type`: `login` | `login_modal` | `captcha` | `challenge`.

In the original harness there was an embedded-browser human-takeover (`browser_request_user_login` → a streamed headed window → `browser_save_auth`). **None of that exists standalone — do not pretend a takeover tool exists.** Your options are honest and limited:

- Record the wall: set the hypothesis `status: blocked` + the right `wall_type`, and write into `goal.md` exactly what is needed ("requires a logged-in session cookie for example.com" / "Cloudflare challenge — needs a real browser session").
- **Pause and ask the user** to supply what unblocks it: a `credentials.json` (for API/login), or — if they can — an exported `storage_state.json` / cookie they obtained in their own browser, which your Playwright probe can then load via `context = await browser.new_context(storage_state="storage_state.json")`.
- If they can't or won't, the source is **blocked**; route it `infeasible` and move on. Never silently ship empty output when a wall is the cause — a `blocked` hypothesis naming the `wall_type` is the correct outcome.

> **Ethics check (once per site):** glance at `robots.txt` and any visible ToS. If the target path is explicitly disallowed or the site forbids scraping, surface that to the user *before* building rather than routing `deterministic` — let them decide.

## ROUTE DECISION — decide ONCE, before building anything

The moment your hypotheses are confirmed (you know **what the data is** AND **how big the full set is**), STOP and pick the terminal route. Reaching for `workflow.py` / a selectors file before this is the #1 anti-pattern — you'll over-engineer a persistent crawler for data you could grab inline, or sink effort into static code a too-dynamic site will defeat.

Run the checklist in order; **first that fits wins:**

| # | Test | Route |
| --- | --- | --- |
| 1 | Full set is TINY (a few dozen records, one page type, harvestable right now in-session)? | **inline** |
| 2 | Control flow TOO DYNAMIC for static code (heterogeneous templates, state-dependent nav, session wall needing periodic takeover)? | **agentic** |
| 3 | NEITHER code nor an agent can crawl it (per-request hard captcha, hard paywall)? | **infeasible** |
| 4 | Otherwise: crawlable, enumerable, static-enough control flow | **deterministic** |

- **inline** — harvest the whole set *now*, this session (httpx or your Playwright probe), and write it straight to `output.json`. No `workflow.py`. The completeness anchor is automatic (the full set = what you scraped) — just be sure you got the WHOLE set, not a sample. If you're unsure whether "small" is small enough, it isn't → deterministic.
- **deterministic** — the common case. Static-enough control flow → Phase 3 writes `workflow.py` + **exactly one** route manifest (`selectors.yaml` for extraction, `download_manifest.yaml` for a downloadable file, or `api_manifest.yaml` for an API). Detailed in `build-validate-run.md`.
- **agentic** — control flow too dynamic for static code, and you can *name* the dynamic that defeats it. In the original harness this dispatched a persistent crawl agent. **Standalone this route is DEGRADED: there is no persistent crawl agent.** Do NOT force a deterministic `workflow.py` onto a too-dynamic site. Instead write a `crawl_brief.md` (seed, what to harvest, field meanings, and a mandatory **completeness anchor** labeled `hard` — an enumerable index with a total — or `soft` — a stop heuristic with no total) and hand back to the user / a real harness if one is present (the SKILL.md capability-probe checks for a sibling `harness/`).
- **infeasible** — only after you actually hit the wall, never preemptively. Record honestly in `goal.md` why (the wall_type, the paywall), mark it off-goal, terminal.

Reach for agentic/inline/infeasible only with a *named* reason; **when in doubt, deterministic.** Then record the chosen route + a one-line rationale into `state.json` and `goal.md`. That recording IS this phase's finish line.

## Pivot rules (and the corrected rationale)

You may have started with Phase 1's guessed type. Switching is allowed within limits:

- **extraction ↔ download** — free. An "embedded" page that's really a CSV link, or a "file" that's really an HTML table, just flips type.
- **api → download** — fine. The API hands you a bulk file URL; both run browserless.
- **api → extraction** — *discouraged.* In the original harness this was hard-*forbidden* because the browser was mechanically disabled in API sessions. **That rationale does not apply to you** — vanilla Claude Code can drive a browser. The real reason to avoid it: if an "API" turns out to be page-embedded data you scrape from the DOM, you almost certainly **mis-identified the source upstream**. Don't quietly build around it — flag it and re-discover the source as an extraction target instead.
- **extraction/download → api** — if you discover a genuine separate API mid-explore, note it in `goal.md` as a newly-discovered source (its own future run, gated on user credentials); don't silently fold it into this run.

## Weak-schema data — when the data doesn't decompose into fields

Not everything is repeating same-shaped rows. A single long report, media items, a heterogeneous corpus, deeply-nested structures — **do not force fake fields, and do not call it off-goal merely because it isn't tabular.** YOU decide the organization. The contract stays `list[dict]`, but one "record" may be ONE DATA UNIT (one document, one media file, one forum post) rather than one table row. One record total is a legal dataset.

Non-negotiable minimum per record, every route:

- **`source_url`** — where this unit came from. Always present.
- **a content carrier** — `content` (inline text, when it fits sanely) OR `file_ref` (a workspace-relative path to a file you saved on disk — PDFs, media, oversized text belong on disk, never inline).
- whatever meaning-bearing fields you judge useful (`title`, `doc_type`, `published_at`) — each carrying a *real* meaning.

Degenerate single-field selectors are LEGAL: for a single-document page the record container may be `article`/`body`, the extractor returns a one-element list, and a `file_ref` field is marked stability-`SKIP` (the check is "file exists + non-empty", not re-extracting its bytes). Two boundaries: if the data's MAIN BODY is a downloadable file the site already offers, that's the **download** route; and if the data is naturally tabular, extract the real fields — weak schema is a fallback, not a shortcut.

**Declare the meaning** in the chosen route's sidecar so a downstream consumer never guesses: extraction → `selectors.yaml`'s per-field `semantic` (write the real meaning of `content`/`file_ref`, e.g. "full report body as markdown"); agentic → the `crawl_brief.md` field schema; inline → a `data_definition` block alongside `output.json`.
