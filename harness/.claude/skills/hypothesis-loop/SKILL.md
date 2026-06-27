---
name: hypothesis-loop
description: Hypothesis-driven exploration loop for crawler discovery (harness variant). Use whenever the user asks to explore a website or design a crawler.
when_to_use: starting or continuing a /explore run; any task that requires turning a seed URL + goal into a working crawler
---

# Hypothesis-driven exploration loop (harness variant)

Same state machine as the lite variant, with two harness-specific moves
woven in:

```
PICK_NEXT -> [skill_list?] -> PROBE -> UPDATE -> [skill_propose?] -> (loop)
                                                                |
                                       hypotheses confirmed -> ROUTE DECIDE
                                                                |
   deterministic -> VALIDATE_E2E      agentic -> write_crawl_brief
   inline -> commit_inline_dataset    infeasible -> report_off_goal
                                                                |
                              declare_crawl_route  <- THE run's ONLY ending
```

**A run ends by declaring its route — there is no [DONE] marker and no other
finish.** `declare_crawl_route(route, rationale)` is your LAST act (for inline,
`commit_inline_dataset` declares it for you). The orchestrator reads the
declared route to decide what happens next — validate→run for deterministic,
dispatching the crawl agent for agentic, terminal for inline/infeasible. **A
session that ends with no declared route is treated as UNFINISHED and bounced
back for another iteration** — silently stopping after writing artifacts wastes
a whole iteration.

DECIDE the route the moment your hypotheses are confirmed (you know what the
data is + how big the full set is), BEFORE producing artifacts. Jumping straight
to `api_manifest_write` / `selectors_write` / `workflow.py` is the #1 mistake —
you'll over-engineer a persistent crawler for data you could have grabbed
inline, or sink effort into a static workflow a too-dynamic site will defeat.
See the ROUTE SELECTION section.

## Workflow type — extraction vs download vs api (decide early; it's a hypothesis)

**These three types live INSIDE the `deterministic` route — they are the
sub-kinds of `workflow.py`, relevant ONLY once ROUTE SELECTION picks
deterministic.** Identifying "this is an api" does NOT mean "write an api
workflow" — it means "IF deterministic is the route, the workflow is an api one."
A tiny full set is `inline` (you scrape it yourself, no workflow); a too-dynamic
site is `agentic` (a brief, not a workflow). So treat the type as a hypothesis to
carry, not a trigger to reach for `api_manifest_write` / `selectors_write`.

A deterministic run produces ONE of three workflow types. `inputs/<site>/goal.md`
states a LIKELY type, but that's a HYPOTHESIS from upstream discovery — confirm it
while probing and switch if the data's real shape differs (within the pivot rules
below):

- **extraction** (embedded page data): `workflow.py` extracts records →
  `output_sample.json`; declare selectors in **`selectors.yaml`** (via
  `selectors_write`). Validated on self-consistency / reproducibility /
  resample / field-semantics.
- **download** (a downloadable file — CSV/JSON/XLSX/parquet/…): `workflow.py`
  fetches the file(s) into `downloads/` and verifies them; declare what it
  downloaded in **`download_manifest.yaml`** (via `download_manifest_write`).
  Validated on file_downloaded / non_empty / format_valid / parses.
- **api** (an HTTP API): `workflow.py` calls the endpoint(s) over HTTP →
  records in `output_sample.json` (same output contract as extraction);
  declare the endpoints + output fields in **`api_manifest.yaml`** (via
  `api_manifest_write` — each endpoint needs a concrete, immediately-callable
  `probe_url`). Probe per the `api-probe` skill (Bash + httpx; browser tools
  are disabled in api sessions). Validated on self_consistency /
  reproducibility / endpoint_match (a live re-call of `probe_url`) /
  secrets_safe (no credential value in any shipped artifact). A 401/403
  wall → mark the hypothesis `blocked` with `wall_type="login"` and tell the
  user which key is missing — never try to register for keys yourself.

The three are **mutually exclusive per run** — produce exactly one of
`selectors.yaml` / `download_manifest.yaml` / `api_manifest.yaml`, matching
the type you confirm; the validator detects the type from which artifact is
present (api > download > extraction if several exist).

**Pivot rules** (which switches are allowed):
- extraction ↔ download: switch freely when the real shape differs (an
  "embedded" page is really a CSV link; a "file" is actually an HTML table).
- api → download: allowed (the API hands you a bulk file URL — both run
  browserless end-to-end).
- api → extraction: **forbidden** — the browser is mechanically disabled in
  api sessions. If the "API" turns out to be page-embedded data, report via
  `report_off_goal` and emit INCONCLUSIVE instead of building around it.
- extraction/download → api: **forbidden inline** — report the API via
  `report_discovered_source(url, types=["api"], note=...)` so it gets its own
  run (API runs are gated on user-supplied credentials upstream).

**Found a DIFFERENT product mid-run?** If you stumble on a data product that's
NOT your assigned one and NOT a sibling run's — a different URL, or a category
nobody here owns — do NOT handle it inline and do NOT pivot to it. Report it
via **`report_discovered_source(url, types, note)`**; the orchestrator stages it
as its own run. Your `goal.md` names which categories sibling runs already own —
only report genuinely-new products.

**Source off-goal entirely?** If, while probing, you judge the source does NOT
serve `goal.md`'s User need at all (wrong subject — the source's data does not
serve the goal's need — or there is no usable data), report
it EARLY via **`report_off_goal(reason, types, page_type, data_type, site_type,
fields, caveats)`** — describe what the source ACTUALLY is — then emit
INCONCLUSIVE instead of building a workflow for an off-topic source. Report ONLY
when off-goal; stay silent when the source is on-goal. (An upstream reviewer uses
your first-hand description to correct the prior that surfaced this source.)

## Weak-schema data — when the data doesn't decompose into fields

Some on-goal sources don't decompose into repeating same-shaped records: a
single long document / report, media items, a heterogeneous corpus (every
page a different shape), deeply nested structures. **Do NOT force fake
fields onto such data, and do NOT `report_off_goal` merely because it isn't
tabular.** YOU decide the organization — the pipeline contract stays
`list[dict]`, but a "record" may be ONE DATA UNIT (one document, one media
item, one corpus page) rather than one table row. One record total is a
legal dataset.

The non-negotiable minimum per record (any route, any workflow type):

- **`source_url`** — where this unit came from. ALWAYS present, per record.
- **a content carrier** — `content` (inline text, when it fits sanely) OR
  `file_ref` (workspace-relative path to a file the workflow saved on
  disk — PDFs, media, oversized text). Media bodies belong on disk, never
  inline.
- whatever meaning-bearing fields you judge useful (`title`, `doc_type`,
  `published_at`, …) — your call, but each must carry a REAL meaning.

Declare what the data IS — the definition — in the route's sidecar, so a
downstream consumer never has to guess:

- **extraction** → `selectors.yaml`'s per-field `semantic` (write real
  meanings, especially for `content` / `file_ref`: "full report body as
  markdown", not "the content").
- **agentic** → the crawl_brief's `field_schema`; the crawl agent carries
  it into `init_crawl(data_definition=...)` and refines it there if the
  real shape differs.
- **inline** → `commit_inline_dataset(data_definition={...})`.
- (agentic/inline definitions land in `runs/<run_id>/manifest.yaml` as
  `data_definition` — that's where downstream readers look.)

Degenerate selectors are LEGAL: for a single-document page,
`record_locator` may be the document container (`body`, `article`),
`extract_js` returns a one-element array, and a `file_ref` field gets
`stability: SKIP` plus a `semantic` saying what file it points at (the
validator checks the file exists + is non-empty instead of re-extracting
its bytes). Two boundaries: if the data's MAIN BODY is a downloadable file
the site already offers, that's the **download** type, not a weak-schema
extraction; and if the data IS naturally tabular, extract real fields —
weak schema is a fallback for genuinely non-tabular data, not a shortcut.

## Hypothesis schema

`workspaces/<site>/hypotheses.yaml` is a top-level YAML list of hypothesis
objects, written through the `hypothesis_append` / `hypothesis_set_status` tools
(not by hand). Source of truth: `mcp_server/schemas/hypotheses.py`. Fields:

- `id` — stable unique identifier (string).
- `claim` — one-sentence claim being tested (string).
- `source` — where the hypothesis came from (string).
- `status` — `unverified` | `confirmed` | `refuted` | `partial` | `blocked`.
- `priority` — `low` | `medium` | `high`.
- `result` — outcome description, or `null`.
- `notes` — free-form context, or `null`.
- `wall_type` — `login` | `login_modal` | `captcha` | `challenge` | `none` | `null`; valid only when `status` is `blocked` (which access wall blocks the hypothesis).

## PICK_NEXT

Highest-priority `unverified` hypothesis. None left -> VALIDATE_E2E.

**If you were started by the explore-loop orchestrator**, the
SessionStart hook injects (when present):

- Your `task_plan.md` — the plan & expectations you wrote for this site,
  carried from the prior iter.
- The previous iter's `## Last iter summary` section from
  `iter_summary.md` (includes its `DO NOT retry next iter` subsection).
- The diff between the previous workflow snapshot and current
  `workflow.py`.
- The latest `validation_report.md` section.
- Outstanding `unverified` + `high` hypotheses.
- `_last_run_status.json`.

A hypothesis can be moved out of `unverified` via the schema-validated
tool — `status` values: `confirmed` / `refuted` / `partial` / `blocked`:

    hypothesis_set_status(
        hypothesis_id="<id>",
        status="<status>",
        result="<one-line outcome>",
        notes="<optional>",
        wall_type="<login|login_modal|captcha|challenge|none>",   # optional; only with status=blocked
    )

Before probing, consider:

- Does any `skill_list` entry's `when_to_use` look applicable?
  -> `skill_read` it, adapt, and record `skill_record_use(success=...)`
  after.
- Does `memory_*.md` mention a related observation?
  -> `memory_read` and let it shape your probe.

## PROBE

Pick the right primitive for the question:

- **`browser_player(script=...)`** for exploratory probes. Async Python with
  `page`, `context`, `cdp` in scope. Assign `result = ...`. The output is
  JSON-serialised so it's safe to read into your next decision.
- **`browser_cdp_send(method, params)`** when you know the exact CDP call
  (Network.enable, Input.dispatchMouseEvent with custom modifiers, ...).
- **`browser_evaluate(expression)`** for simple JS one-liners.
- **`browser_goto`** for navigation; pair with `wait_until` carefully.

Always end with `browser_snapshot(name=...)`.

## Access walls (login- OR verification-gated data)

The goal is the DATA. Whenever you **can't get the data you came for** — not
only when a page is blank — suspect a wall. Triggers:
- a `browser_goto` lands on empty / blocked content, or redirects to a login URL;
- the page looks normal but the **records / fields you need are missing, blurred,
  truncated, or behind a "sign in to continue" modal/overlay**;
- a **CAPTCHA**, a "verify you are human", or a **Cloudflare "just a moment"**
  challenge sits between you and the data.

Don't guess — probe, then escalate:

1. `browser_check_login_wall()` → `{is_access_wall, wall_type, is_login_wall,
   signals}`. `wall_type` ∈ `login` / `login_modal` / `captcha` / `challenge` /
   `none`. The rich-content guard still means a normal page with a mere "Sign
   in" link is NOT flagged — but a login *modal over* content, a captcha, or a
   JS/Cloudflare challenge IS.
2. If `is_access_wall` (any `wall_type` ≠ `none`), hand over to the human — the
   SAME takeover covers logging in AND solving a captcha / challenge:
   - `browser_request_user_login(url, reason="<what's blocking + what to do>")`
     opens a VISIBLE window. Then **STOP your turn** and tell the user exactly
     what to do there ("Log in" / "Solve the verification") and to say continue.
     Don't poll.
   - On the next turn, `browser_save_auth()` persists the result — login cookies
     AND any verification clearance (e.g. `cf_clearance`) — and re-authenticates
     the main browser. Re-fetch — the data should now be reachable.
3. Whether a human is reachable is not determined by whether the run is
   orchestrator-driven; `browser_request_user_login` resolves it at call time.
   With a takeover channel present, the call streams the headed login browser to
   the human, blocks until they finish, and persists the resulting auth. With no
   takeover channel present, the call returns `{autonomous: true}`.

Never silently ship empty / partial output when a wall is the cause — a
`blocked` hypothesis that names the `wall_type` is the correct outcome.

`auth_state.json` is a SINGLE project-global file — one login store shared by
all workspaces; cookies for multiple sites accumulate in it (merged on save).
It's reused automatically: the main browser (`browser_attach` loads it), the
generated `workflow.py` (resolve it by walking up from `__file__`, pass as
`storage_state`), and the validator's re-fetch. It holds live cookies —
gitignored; never paste its contents anywhere.

## UPDATE

- **confirmed**: write the evidence into `hypotheses.yaml` (verification
  snippet, response excerpt, snapshot filename). Also call
  `workspace_append_facts`.
- **refuted**: write the counter-example and why you guessed wrong.
- **partial**: split into sub-hypotheses with `parent_id` set.
- **blocked**: log what you saw and leave a TODO.

Append a section to `exploration_log.md` via `workspace_append_log`.

If this probe materially changed the plan — a field turned out
unextractable, the crawl method differs from what you intended, scope
shifted — `workspace_write` an updated `task_plan.md` so it stays current.

If a stable function emerged, add it via `workspace_helper_append(name, code)`.
Pick a unique name; helpers.py is append-only. Don't try to overwrite.

If you discovered a *transferable* technique:

```text
skill_propose(
    skill_id="<kebab-case-id>",
    title="<short title>",
    when_to_use="<the trigger condition>",
    description="<the approach>",
    evidence="<what proved it on the current site + where the snippet is>",
    recipe="<async function source>",
)
```

Only propose after the technique demonstrably worked on the current site.

## Selector tracking (for downstream validation + cross-site catalog)

When you've finalized the selectors workflow.py uses to extract listing
records, write them to **`workspaces/<id>/selectors.yaml`** via the
`selectors_write` MCP tool (NOT into hypotheses.yaml — that sidecar
was migrated 2026-05-22 to fix the schema-collision between hypothesis-
loop and validation-agent).

`selectors.yaml` is **the canonical source the validator re-extracts
from** — the downstream validation agent re-fetches using these
selectors (NOT workflow.py's inline copy), so writing them correctly
here is critical.

If `workflow.py`'s inline selectors drift from this block, the
validator's re-extraction won't reproduce workflow.py's output — a
signal worth surfacing ("workflow.py uses different selectors than
selectors.yaml declared"). The `workflow_field` keys let the validator
catch this drift by NAME, not only by mismatched values.

Schema:

```yaml
selectors:
  observed_at: <ISO timestamp>
  source_url: <url where selectors were validated>
  records_observed: <int, how many records the locator matched>

  # The CSS selector for "one record container"
  record_locator: '<css selector>'

  # Per-field selector + extraction method (for LLM audit + cross-site
  # pattern detection). `selector` is relative to a record container.
  # The KEY is the CANONICAL field name (what goal.md / output schema calls it).
  fields:
    <field_name>:
      selector: '<css>'
      extraction: '<short description>'   # e.g. "textContent.trim()" or "href"
      fallback_selector: '<css>' | null
      stability: stable | positional | fragile
      observation: '<one-line note>'
      workflow_field: '<name>' | null     # the name workflow.py uses for this
                                          # field. null = same as the key above
                                          # (common case). Set explicitly ONLY when
                                          # workflow.py uses a different name, so the
                                          # validator can flag naming drift.
      semantic: '<what this field really is>'  # unambiguous real-world meaning,
                                          # e.g. "upvote score, NOT comment count".
                                          # Lets the validator confirm the selector
                                          # maps to the RIGHT field (catches e.g. a
                                          # comment-count selector wired to votes).

  # Complete JS function string the validator evaluates via page.evaluate().
  # MUST return a list[dict] with same field names as the workflow output.
  # Mirrors workflow.py's inline JS — keep them in sync.
  extract_js: |
    () => {
      const cards = document.querySelectorAll('<record_locator>');
      return Array.from(cards).map(unit => {
        const titleEl = unit.querySelector('<field selector>');
        // ... full extraction logic ...
        return { field1: ..., field2: ..., ... };
      });
    }
```

Common selector pitfalls — capture these in `semantic` / `stability` so the
validator can catch them:

- **Positional fields**: when several same-typed values share one record
  container and a single selector matches all of them, an index selects one.
  Record which index maps to which field in `semantic` and set
  `stability: positional`; swapping indices is a silent bug.
- **Primary + fallback**: a field may read from a structured/embedded data
  source, with `fallback_selector` covering DOM variants.
- **`workflow_field`**: set it only when `workflow.py` names the field
  differently from the canonical key, so the validator can flag naming drift.

**Why both structured fields AND extract_js**: the structured fields
(`selector` / `workflow_field` / `semantic` / `stability`) are for the
**validator's audit** — it cross-checks that workflow.py wired each
selector to the right field name (`workflow_field`) and the right
meaning (`semantic`), so a field's selector isn't silently scraping a
neighbouring value. The `extract_js` is the **runtime re-extraction**
code the validator `page.evaluate()`s to ground-truth a sample of
records against output_sample.json.

## Field stability tracking (for downstream validation)

When a field is `confirmed` extractable, ALSO observe its **stability
behavior** across multiple re-fetches of the same source. This feeds
the downstream validation-agent's field classification (STRICT /
TOLERANT / SKIP) — its accuracy depends on your observations.

**How to probe**:

Before declaring all field hypotheses confirmed, do ONE stability pass:

1. Pick a single representative card / record (e.g. the first listing
   result, or a known-stable post URL).
2. Re-extract the same fields 2-3 times across short intervals (or
   across page transitions in a multi-page workflow). For animated
   counters (likes, scores), this surfaces drift.
3. Note for each field whether the value is **stable**, **drifting** (numeric
   movement within ~5%), or **textual_drifting** (e.g. "3 hours ago" → "4 hours ago").

**Where to record**:

Write the `field_stability:` block to **`selectors.yaml`** (alongside the
`selectors:` block) via the `selectors_write` MCP tool — NOT
`hypotheses.yaml`. Selectors + field_stability both migrated to
`selectors.yaml` on 2026-05-22 (see "Selector tracking" above); the
`SelectorsFile` schema requires both blocks there. Shape:

```yaml
field_stability:
  observed_at: <ISO timestamp>
  source_url: <url you re-probed>
  probe_count: <int>
  fields:
    <field_name>:
      drifted: <bool>
      drift_examples: [<v1>, <v2>, <v3>]   # include when drifted: true
      observation: <one-line note>
      suggested_class: STRICT | TOLERANT | SKIP
```

**Classification suggestion legend**:
- `STRICT` = identifier or stable text; any change = workflow bug
- `TOLERANT` = numeric with real-world drift; ±5% tolerable
- `SKIP` = workflow-internal / CDN-cached / time-relative / detail-only

The next `/validate-run` reads this block and uses it as the primary
source for field classification (your observations beat heuristics).
Skipping this step degrades validation accuracy from ~95% → ~80%.

## ROUTE SELECTION — pick the terminal route (the run's exit contract)

The moment your hypotheses are confirmed — you know what the data is AND how big
the full set is — STOP and DECIDE the terminal route, BEFORE reaching for
`api_manifest_write` / `selectors_write` / `download_manifest_write` /
`workflow.py`. Then produce that route's artifact, and **END the run with
`declare_crawl_route(route, rationale)` — declaring IS the finish** (no [DONE]
marker exists; the orchestrator bounces back a session that ends undeclared).
Declaring early (right after deciding) is also fine — the declaration persists;
what matters is that the run never ends without one.

Match the route to what you found — the four are PEERS: no default, no fallback,
no route you have to "earn." Each fits a different reality; pick the one that
matches yours and **end the run with `declare_crawl_route(route, rationale)`,
where the rationale says WHY that route fits what you saw**. EVERY route needs a
rationale — the orchestrator rejects an undeclared run, and a declaration with no
stated reason is half the job, whichever route it is.

The four, each with the reality it fits:

- **deterministic** — control flow is static enough for code: stable pagination,
  stable selectors, homogeneous pages. Produce `workflow.py` + its manifest;
  declare with a rationale naming what makes it reproducible standalone. Proceed
  to VALIDATE_E2E below (write workflow.py → validate → run).
- **agentic** — control flow is too DYNAMIC for static code: heterogeneous page
  templates, state-dependent navigation, a session wall needing periodic human
  takeover, anti-bot needing human-like variation. Skip workflow.py; instead
  `write_crawl_brief(...)` — the persistent agentic crawl agent harvests it.
  Declare with a rationale saying why static code won't hold here (no special
  burden of proof — state the reason, the same as for any route). The brief's
  COMPLETENESS ANCHOR is mandatory; label its hardness honestly (hard = enumerable
  index with a total → set `estimated_total`; soft = stop heuristic, no total →
  completion is subjective). It is how the agent judges "done".
- **inline** — the FULL SET is small enough to harvest right now, in THIS
  session: typically a few dozen records, one page type, no persistent run
  warranted. Scrape it with your browser tools and `commit_inline_dataset(records)`
  — it produces the dataset directly (same artifact the agentic agent would,
  consumed by the Data Agent), no downstream. The anchor is automatic (full set =
  what you scraped); make sure you got the WHOLE set, not a sample. Declare with a
  rationale for why a persistent pipeline is wasteful for so little data.
- **infeasible** — NEITHER code nor an agent can crawl it: per-request hard
  captcha, paywall, real-human verification. `report_off_goal(...)`; declare with
  a rationale describing the wall you actually hit, not a preemptive guess. Don't
  burn a run.

A tiny full set can be read as inline OR deterministic — both can crawl it; choose
by the trade-off (inline when a persistent pipeline is genuinely wasteful,
deterministic when it's worth standing up) and name that trade-off in the
rationale. If you still can't tell what the data is or how big the full set is,
you are not done confirming hypotheses — keep probing; the route decision waits
until you can justify whichever route you pick.

## Finishing your declared route

Each route finishes differently — do the one you declared. They are PEERS, not a
main path plus exceptions. Section length below reflects how much an artifact
needs spelling out, not a route's importance — a longer section is not a more
important route.

- **deterministic** → VALIDATE_E2E, below: write `workflow.py` (+ the
  `selectors.yaml` / `download_manifest.yaml` / `api_manifest.yaml` it needs) →
  validate → run.
- **agentic** → `write_crawl_brief(...)` with the mandatory completeness anchor
  (hardness-labeled) + `field_schema`. That hands off to the Agentic Crawl agent,
  whose harvest procedure is its OWN skill (`agentic-crawl`). You finish here by
  writing the brief.
- **inline** → `commit_inline_dataset(records)` with the WHOLE set you scraped in
  this session; it produces the dataset directly, no downstream. You finish here
  by committing it.
- **infeasible** → `report_off_goal(...)`; done (a give-up declaration — no
  artifact to build).

How each route is verified:

- **deterministic** — YOUR step: the validator agent re-runs `workflow.py`
  standalone and re-fetches the live page (VALIDATE_E2E below — resample_match /
  field_semantics / reproducibility / …); you iterate until it PASSes.
- **agentic** — not verified here. The Agentic Crawl agent harvests + judges
  completeness against the brief's anchor, and the run-manifest gate computes the
  outcome (produced_output / completeness / self_consistency / secrets_safe). That
  procedure is the `agentic-crawl` skill, not this section.
- **inline** — `commit_inline_dataset` runs the finalize gate on what you committed
  (produced_output / self_consistency / secrets_safe / completeness) and the
  orchestrator spot-checks the deliverable. Mechanical, no separate validator.
- **infeasible** — nothing to verify (no data produced).

### VALIDATE_E2E — the deterministic route's steps

(Only the `deterministic` route uses this subsection; agentic / inline /
infeasible finished above.)

1. Write `workspaces/<site_id>/workflow.py`. Importable structure:
   ```python
   from helpers import *   # or import specific helpers
   ```

   **`CRAWL_MODE` — sample vs full.** workflow.py reads the `CRAWL_MODE` env var
   and parameterizes its crawl extent by it (same code + selectors; only how far
   it goes differs):

   ```python
   import os
   CRAWL_MODE = os.environ.get("CRAWL_MODE", "sample")
   MAX_PAGES = None if CRAWL_MODE == "full" else 2   # None = crawl to exhaustion
   ```

   - `sample` (default): a small, bounded, fast representative crawl. The bare
     `python workflow.py` and the validator both run this — keep it cheap.
   - `full`: lift the caps to `goal.md`'s complete scope. The Run agent
     (`runtime.cli run`) sets this to produce the kept dataset.

   What "full" means is site-specific (all pages until no `next` link, a date
   window, a category sweep) — define it and record it under "Intended crawl
   method" in `task_plan.md`. The output filename stays `output_sample.json`
   either way; the Run agent runs full mode in an isolated `runs/<run_id>/` and
   keeps the result there as `output.json`, never overwriting this sample.

   **`WORKFLOW_CDP_PORT` — remote-debuggable browser (production runs).** If
   the workflow drives a browser, honor this env var by passing it to
   chromium's launch args:

   ```python
   HEADLESS = os.environ.get("HEADLESS", "1") != "0"   # the Run agent sets this
   _cdp = os.environ.get("WORKFLOW_CDP_PORT")
   browser = await pw.chromium.launch(
       headless=HEADLESS,
       args=[f"--remote-debugging-port={_cdp}"] if _cdp else [],
   )
   ```

   The Run agent (`runtime.cli run`) sets it so a mid-crawl login / captcha
   wall can be STREAMED to the watching user, who solves it inside the LIVE
   crawl browser (same context — your session continues authenticated).
   Unset (bare runs, the validator) → empty args, nothing changes. A workflow
   with no browser (pure httpx) ignores this var entirely — api workflows
   skip it by design.

   **Heartbeat (api workflows).** The production runner kills a crawl after
   10 minutes of stdout silence. An api workflow that sleeps through
   rate-limit windows must still `print()` a one-line progress/heartbeat at
   least every ~60s (records so far, current page, "sleeping 30s on 429"),
   including DURING the sleeps.

   **Incremental output flush (production-kill safety).** The production run
   can be terminated at ANY moment — wall-clock cap, stall timeout, or a
   deliberate mid-crawl abort — and on Windows termination is immediate (no
   signal grace): anything held only in memory is lost. workflow.py must
   NEVER keep the only copy of scraped records in RAM — rewrite the output
   file (atomically: write `.tmp`, then `os.replace`) every ≤50 records or
   ≤60s, whichever comes first:

   ```python
   def flush(records, path="output_sample.json"):
       tmp = path + ".tmp"
       with open(tmp, "w", encoding="utf-8") as f:
           json.dump(records, f, ensure_ascii=False, indent=2)
       os.replace(tmp, path)   # atomic — a kill mid-write never corrupts the file
   ```

   A killed run that left partial output on disk gates to PARTIAL (data
   salvaged) instead of ABORTED (nothing kept).
2. Run with Bash: `python workspaces/<site_id>/workflow.py`.
3. Compare against `goal.md`. Missing field -> back to PICK_NEXT with a
   targeted hypothesis.
4. **Field stability pass**: see "Field stability tracking" section
   above. Write the `selectors:` + `field_stability:` blocks to
   `workspaces/<id>/selectors.yaml` via `selectors_write` MCP tool
   before declaring DONE. **API workflows instead** re-call the endpoint
   2–3 times, classify each declared field the same way (STRICT / TOLERANT
   / SKIP), and write everything via `api_manifest_write` — `fields[]` with
   `stability`, a concrete `probe_url` per endpoint, `identifier_field`,
   `pagination`, `rate_limit`, and `credentials_source` (a POINTER like
   "env API_KEY, else credentials.json walk-up" — NEVER the value).
5. **Check `task_plan.md` against `selectors.yaml`, fix only mismatches**:
   the "Target data & fields" in task_plan was an intent written before
   probing. Compare it to the confirmed `selectors.fields` keys; if they
   agree, leave it untouched. Only where they differ, `workspace_write` a
   correction — drop unextractable fields, add ones you discovered, fix
   any field whose real meaning differs (per its `semantic`), keeping BOTH
   the annotated field table (`字段 | 说明`) and its `fields` block in
   sync. Same check
   for "Intended crawl method" / "Success criteria". A consistency check,
   not a mandatory rewrite — the plan carried to the next iter just must
   not misdescribe what the crawler does.
6. **Write the iter_summary section** via `iter_summary_append` MCP tool
   (REQUIRED — this is the primary memory carrier across iters in an
   orchestrator-driven loop). Be specific in `do_not_retry` (name the exact
   dead-end and why, not a vague "X didn't work"). Be specific in
   `next_strategy` (a structurally different direction, not "try harder").

   ```text
   iter_summary_append(
       tried=["<hypothesis id + what you changed>", ...],
       worked=["<what produced a result>", ...],
       do_not_retry=["<specific dead-end + why>", ...],
       open_hypotheses=["<id (status, priority)>", ...],
       next_strategy="<a structurally different direction>",
       cost_usd=<float>,
       record_count=<int>,
   )
   ```

7. Reflect on whether to `memory_append` a cross-site observation, and
   whether to promote a memory note to a skill.
8. **End the run by declaring the route**: `declare_crawl_route(
   "deterministic", rationale)` — your LAST tool call (there is no [DONE]
   marker; the declaration IS the finish; the orchestrator bounces back a
   session that ends without one). For the other routes the ending is the
   same call after that route's artifact — write_crawl_brief → declare
   agentic; report_off_goal → declare infeasible; commit_inline_dataset
   declares inline for you. **If orchestrator-driven**, exit cleanly after
   declaring — the orchestrator routes what happens next (validation for
   deterministic, the crawl agent for agentic). Do NOT call /explore
   yourself.

## Healing on probe failure

When a probe fails:

1. `browser_snapshot` the failed state first.
2. Try one alternative: different selector, different network event,
   different timing. Log both attempts.
3. If still failing after two retries, mark `partial` or `blocked`, write
   what you observed, and move on. Do not get stuck.

## Anti-patterns

- Touring URLs that don't gate the goal.
- Confirming a hypothesis from Firecrawl alone.
- Skipping `skill_list` at the start of the run.
- Calling `skill_propose` on the first site that exhibits a pattern -
  wait until it's reproduced.
- Editing `exploration_log.md` or `helpers.py` directly via Edit/Write -
  use the append-only tools.
