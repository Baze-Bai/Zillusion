# CLAUDE.md

Browser-driven crawler exploration with CDP-first surface and a cross-site
skill library. The browser is exposed through the `browser-harness` MCP
server in `mcp_server/`.

## What you would get wrong without this

- **Login / auth walls: ALWAYS use `browser_request_user_login`. NEVER DIY.**
  It is the only supported path past a login / captcha / challenge wall — it
  opens a headed browser on a managed X display and STREAMS it to the user.
  Do NOT write your own login scripts, call the site's login / QR APIs (e.g.
  `getQrCode`), decode QR images, or launch your own `chromium headless=False` —
  those bypass the managed display + streamed canvas, trip anti-bot, and the
  user never sees a window (this exact mistake left Trip.com login unsolved). On
  `{ok: false, reason}`, relay the reason and retry; a prior `blocked` / `no X
  server` is NOT permanent — the streamed takeover works, call the tool again.
- **`workspaces/<id>/exploration_log.md` is append-only.** Use
  `workspace_append_log`. A `PreToolUse` hook blocks direct `Edit`/`Write`.
- **`workspaces/<id>/helpers.py` is append-only.** Use
  `workspace_helper_append`. When a helper needs to change, add `_v2` -
  do not rewrite. Same `PreToolUse` hook enforces this.
- **`workspaces/<id>/task_plan.md` is the run's plan doc and is freely
  mutable** (NOT append-only, unlike the two above). It holds the run's
  intended data / fields / crawl-method + expectations. Read it with
  `workspace_read` and rewrite it with `workspace_write` as the plan
  evolves (using those MCP tools rather than `Edit` sidesteps the
  read-set caveat below).
- **`inputs/<id>/seed.json` is a *hypothesis*, not a fact.** Firecrawl
  output is a prior; verify each non-trivial claim.
- **`workflow.py` runs in three contexts, and a virtual display is
  guaranteed in all of them.** Your exploration probes use the managed
  `browser-harness` browser; the validator reruns `workflow.py`
  **standalone** (no MCP browser); the production run does too. The
  container entrypoint starts Xvfb everywhere, so `headless=False` never
  crashes for lack of a display — the browser mode is a FREE choice (the
  harness sets `HEADLESS` / `WORKFLOW_CDP_PORT`, production currently
  headed; VALIDATE_E2E in the `hypothesis-loop` skill shows how a workflow
  reads them). Two consequences: (1) a mechanism that only works inside the
  long-running MCP browser (e.g. a scroll / lazy-load the managed browser
  triggers but a fresh standalone Playwright does not) will NOT reproduce
  in the validator / production rerun, so it cannot carry a `deterministic`
  route — that is what the `agentic` route is for; (2) the headed browser
  here renders via Xvfb software GL (SwiftShader), which is itself
  fingerprintable, so headed is not automatically a stealth win.
- **Sites may serve degraded content or cap enumeration to automated /
  headless clients** — e.g. stripped HTML, a lazy-load that won't arm, a
  per-query result ceiling, a benign overlay that eats a scroll, etc. Stay
  mindful of anti-bot and be ready to address it (e.g. going headed, or
  routing to `agentic` for collection that needs human-like variation).
  But a thin or empty result is only ONE signal of it — weigh it against
  your own environment (e.g. nav flow, headless fingerprint, an open
  dialog, missing-data vs blocked-data, etc.) before concluding the
  mechanism itself is impossible.
- **A `do_not_retry` entry (in `iter_summary.md`) is a config-scoped
  observation, not a proven law.** Each iteration is a fresh session that
  inherits the prior iter's `do_not_retry` as a prior (the SessionStart
  hook injects it). It records "X did not work the way I ran it" under one
  configuration — not "X is impossible" — so a later iter may revisit it
  under a different configuration when that is warranted.
- **Prefer `browser_player` and `browser_cdp_send` over high-level wrappers**
  when the question warrants it. `browser_player` runs async Python with
  `page`/`context`/`cdp` in scope - use it for one-shot probes before
  committing a helper.
- **`workspace_write` does NOT enter Claude Code's Edit tool's read-set.**
  Edit's "must read before edit" check tracks Read / Write tool calls
  only, not MCP writes. Subsequent Edit on a file written via
  `workspace_write` will fail unless the file is independently Read first.
- **You have ONE vision tool, and which one depends on the base-model mode**
  (`runtime/modality.py` — two modes, mutually exclusive). Both turn the live
  page's PIXELS into a usable signal beside the DOM/HTML — reach for it when the
  rendered pixels carry something the markup doesn't (canvas / chart / image-baked
  text, a visual layout, or an overlay/captcha state vs what the DOM claims):
  - MULTIMODAL base model (Claude / Gemini / GPT-4o+ / `*-vl`) → **`browser_screenshot`**
    returns the page as an IMAGE you SEE directly.
  - TEXT base model (e.g. DeepSeek) can't see images → **`browser_vision_probe`**
    instead: it screenshots the page, asks a multimodal SUBMODEL your question,
    and returns a short TEXT answer (the submodel is your eyes; pass a SPECIFIC
    question, it's one paid call). Needs a multimodal backend
    (`CRAWLER_EXPLORER_VISION_MODEL` / `ANTHROPIC_BASE_URL`); degrades to a text
    reason if none is configured.
  Both are EXPLORE-TIME aids only — like a screenshot they can't ride a
  `deterministic` workflow.py (it reruns standalone, no LLM). Turn what you learn
  into a deterministic mechanism (selector / JS read / OCR) or carry it on the
  `agentic` route. Whichever tool is absent, the other mode's is active.
- **`browser_content` has no server-side char cap — use `selector` to
  scope.** No `selector` returns the full page HTML, which on heavy
  sites (Reddit, X, Amazon) will hit the SDK's 25K-token tool-result
  cap. When that happens the SDK auto-saves the result to a file and
  hands you the path — recoverable via Read+limit / Grep, but costs an
  extra round-trip. Pass `selector="main"` / `selector=".search-result-
  link"` etc. to grab a sub-tree on the first call. The response
  includes `chars_total` so you know how big the actual content was.

## Python environment — ALWAYS use `.venv\Scripts\python.exe`

This project has a **dedicated venv** at `.venv/` (created 2026-05-23).
All Python execution — from `workflow.py`, from `runtime.cli` (incl. the
`runtime.validate` validator it spawns), from the MCP server itself
(`.mcp.json` pins to venv python by absolute path) —
**must use the venv python**:

```cmd
.venv\Scripts\python.exe workflow.py
.venv\Scripts\python.exe -m runtime.cli explore-loop <site>
.venv\Scripts\python.exe -m pip install <new-pkg>
```

**Why this matters**: global miniconda might have different / missing
deps. The venv has `yt-dlp` / `httpx` / `playwright>=1.60` /
`claude-agent-sdk>=0.2` etc. set up against `pyproject.toml`. If you
`python workflow.py` (without `.venv\Scripts\` prefix) you'll resolve
to global miniconda where deps may be out of date or absent.

**To install a new dep needed by workflow.py** (e.g. an OCR lib, an
HTML parser, a video format handler):

```cmd
.venv\Scripts\python.exe -m pip install <pkg>
```

Then update `pyproject.toml` `dependencies` so the dep is reproducible
in fresh checkouts. Don't `pip install` into global miniconda — it
won't be visible to the orchestrator / MCP server / workflow.py.

**System binaries** (`ffmpeg`, etc.) are separate from Python deps and
already on PATH for this machine; if you need a new system binary, ask
the user to install it (`winget install <id>`) — agents don't
auto-install at the OS layer.

## When user says "explore X"

Run `/explore <X>`. Everything else flows from the `hypothesis-loop` skill.

## Workflow types — extraction | download | api

A run produces ONE of three workflow types, detected from which workspace
artifact exists: `selectors.yaml` (extraction) / `download_manifest.yaml`
(download) / `api_manifest.yaml` (api); api > download > extraction when
several exist. API-staged sites carry `inputs/<site>/api_spec.json` (the
bridge-written merged spec — also the input-side type marker) and, when the
API needs a key, the user-supplied `inputs/<site>/credentials.json` (both
under the gitignored `inputs/`). API explore sessions run with the browser
tool surface removed (`disallowed_tools` + a `CRAWLER_EXPLORER_WORKFLOW=api`
backstop inside the MCP server's Browser); workspace binding goes through
`workspace_attach`, probing through Bash + httpx (`api-probe` skill), and
the manifest through `api_manifest_write`. Validation adds `endpoint_match`
(live re-call of each endpoint's `probe_url`) and `secrets_safe` (no
credential literal in shipped artifacts) gates.

Records need not be table rows. For data that doesn't decompose into
fields (single documents, media, heterogeneous corpora) a record is one
DATA UNIT: minimum metadata per record is `source_url` + a content carrier
(`content` inline or `file_ref` to a file on disk); the meaning of each
field is declared in the route's sidecar — `selectors.yaml` `semantic`
(extraction), the crawl_brief `field_schema` (agentic), or the
`data_definition` param of `init_crawl` / `commit_inline_dataset`, which
lands in `runs/<run_id>/manifest.yaml`. See the `hypothesis-loop` skill's
"Weak-schema data" section.

## Terminal routes — how an /explore run ends

An /explore run ends by declaring its terminal route
(`declare_crawl_route` → `workspaces/<id>/crawl_route.json`) — there is no
`[DONE]` marker. Four routes; the explore-loop orchestrator
(`runtime/explore_loop.py`) reads the file and branches:

- **deterministic** — workflow.py written; the loop validates → run (below).
- **agentic** — `write_crawl_brief` wrote `crawl_brief.md` (mandatory
  completeness anchor, hardness-labeled); the loop dispatches the Agentic
  Crawl agent (`runtime/crawl_agent.py`, also manual:
  `.venv\Scripts\python.exe -m runtime.cli crawl <site_id>`), which harvests
  into `runs/<run_id>/records.jsonl` → `output.json`; COMPLETE maps to PASS.
- **inline** — explore harvested a tiny full set itself via
  `commit_inline_dataset` (same `runs/<run_id>/output.json` + manifest shape
  as the agentic agent); the loop spot-checks the deliverable, no validator.
- **infeasible** — `report_off_goal` written; the loop ends INCONCLUSIVE.

A session that ends with NO declared route is treated as unfinished — the
loop queues a first-turn notice and iterates.

## Running the finalized workflow (production crawl)

The terminal pipeline stage after a workflow PASSes validation:
`.venv\Scripts\python.exe -m runtime.cli run <site_id>` runs `workflow.py` at
FULL scope (`CRAWL_MODE=full`) to produce + KEEP the dataset under
`workspaces/<id>/runs/<run_id>/` (`output.json` + `media/` + `manifest.yaml` +
`report.md` + `feedback.yaml` + `crawl_stdout.log`). The Run agent
(`runtime/run_agent.py`) mirrors the validator's isolation — a self-contained
SDK session, read-only on explore artifacts, writes only `runs/`. The
deterministic core (`runtime/run_exec.py`) streams the crawl and enforces the
wall-clock + stall kill thresholds; the outcome is gate-computed in
`manifest.yaml` (COMPLETE / PARTIAL / FAILED / ABORTED). A non-COMPLETE run or
its `feedback.yaml` is surfaced to the next `/explore` by the
`inject_run_feedback` SessionStart hook. `workflow.py` selects sample-vs-full
via the `CRAWL_MODE` env var (see the `hypothesis-loop` skill's VALIDATE_E2E).

## Consuming the data (Data Agent)

The pipeline's 5th stage CONSUMES the crawled data:
`.venv\Scripts\python.exe -m runtime.cli data --sources <site_id|site_id:run_id|path> [...] --task "<what to build>"`
cleans one or more crawled datasets (cleaning is USER-DIRECTED — via `--task`
or `--clean-spec` — and audited to `cleaning_recipe.yaml`) and builds open-ended
data products (reports, charts, derived datasets, decks, spreadsheets) under
`products/<product_id>/`. Unlike the validator/run agents (narrow MCP isolation),
the Data Agent (`runtime/data_agent.py`) is the INVERSE — a full-capability
`claude_code` session built via `options.build_options`, grounded on real data,
with an ADDITIVE `data` MCP server (`runtime/data_tools.py`) for profiling /
audited cleaning / product registration. Sources are staged read-only into
`products/<id>/sources/`; the agent writes only under `products/<id>/`. The
completion `outcome` is gate-computed in `manifest.yaml` (COMPLETE / PARTIAL /
FAILED / ABORTED — judges completion, never product quality). See the
`data-product` skill. Not wired into the backend yet (run it manually).

## Learning - three layers

| Layer | Path | Lifetime | Promoted from | Promoted to |
| --- | --- | --- | --- | --- |
| Per-site helpers | `workspaces/<id>/helpers.py` (append-only) | one run | hypotheses | — |
| Cross-site notes | `memory/*.md` | grows | reflection | a skill |
| Cross-site skills | `domain_skills/<id>/` | grows | a memory note | — |

`.claude/skills/skill-curator/SKILL.md` has the promotion rules.

## Verification

- `ruff format`, `ruff check mcp_server`
- `python -m mcp_server.server` boots the stdio server (Ctrl-C to exit)
