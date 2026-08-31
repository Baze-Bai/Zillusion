# Zillusion — Data Source Discovery & Scraper-Building Agent

Turn a plain-language data need into **a vetted list of data sources, a working
validated scraper, or a finished dataset** — driven by an LLM agent end to end.

[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)
[![Stars](https://img.shields.io/github/stars/Baze-Bai/Zillusion?style=social)](https://github.com/Baze-Bai/Zillusion/stargazers)

![Zillusion demo](docs/demo.gif)

*One sentence in — "global CO2 emissions by country" — and it ranks 13 real sources
on 5 dimensions, builds a scraper for the one you pick, validates it, and runs it to
a downloadable dataset. Real capture; the address bar says `127.0.0.1` because that
is where this runs.*

## Why this and not a crawler

Finding the **right** source is the hard part, and the scraper you write breaks on
the next redesign. Two things here exist to attack exactly that, and they are the
parts worth stealing even if you use something else:

- **The validator cannot rubber-stamp its own work.** A scraper is graded by a
  *separate, read-only* agent session that physically cannot edit the workflow it is
  judging. Its sharpest check re-reads the **live page** to confirm each field means
  what the selector claims — the failure it catches is a selector quietly bound to
  upvotes instead of comments. The verdict is computed from a scorecard, not argued
  by the model.
- **Anti-bot knowledge accumulates instead of being rediscovered.** Real, dated
  notes — CDN signature URLs, fresh-context fingerprinting, soft login gates,
  hydration traps — persist across runs and are promoted to cross-site skills once a
  pattern repeats. Most tools relearn every block from zero on every run.

You ask *"where can I get data about X?"* and the agent runs the pipeline:

```
discover → explore → validate → run → (data)
```

1. **Discover** — finds and ranks candidate sources (APIs, downloadable files,
   embedded tables) from source registries + web search.
2. **Explore** — for a source you pick, probes the site and writes a runnable
   scraper (`workflow.py`).
3. **Validate** — runs the scraper and checks its output against the live page.
4. **Run** — executes the validated scraper at full scale → a downloadable dataset.
5. **Data** *(optional, harness CLI)* — cleans the data and builds a data product
   (report, chart, dataset).

> ⚠️ **Security model:** ships configured for **single-tenant, localhost use**
> (no built-in user accounts). Before exposing it on a network, read
> [SECURITY.md](SECURITY.md).

## Features

- **End to end**: question → ranked sources → scraper → dataset.
- **Three source types**: APIs, downloadable files, and embedded / HTML-table data.
- **Real-time steering**: chat to guide or correct the agent mid-run.
- **Human takeover**: login / captcha walls hand control to an embedded browser.
- **Credentials gate**: keyed APIs prompt you for the key (never self-registered;
  the key never leaves the backend).
- **Emit-as-you-go**: long crawls stream results to disk; datasets are downloadable.
- **Bring-your-own model**: DeepSeek, GLM (Zhipu), Claude, OpenAI, … via LiteLLM
  routing; the agentic node uses the Claude Agent SDK over an Anthropic-compatible
  endpoint.
- **No search keys by default**: self-hosted SearXNG meta-search + free source
  registries; optional commercial providers (Brave / Tavily / Exa) if you add keys.

## Three ways to use it

| Form | For whom | What you run | Weight |
|------|----------|--------------|--------|
| **① Self-hosted web app** | Most users — full experience | `docker compose up`, browse `localhost:3000` | Heavy (full stack) |
| **② Harness CLI** | Developers, scripting, CI | `python -m runtime.cli <cmd> <site>` | Medium |
| **③ Claude Code skill** | Anyone already using Claude Code | drop `skills/find-and-scrape-data` into `.claude/skills/` | Light (near-zero infra) |

All three share the same harness core. **You always bring your own LLM API key**
(DeepSeek / GLM / Claude / OpenAI / …) — none is bundled.

---

## ① Self-hosted web app (quickstart)

**Prerequisites:** Docker + Docker Compose, and at least one LLM provider API key.

```bash
git clone <your-repo-url> zillusion && cd zillusion

# 1. Compose-level config
cp .env.example .env
#    set SEARXNG_SECRET   (openssl rand -hex 32)

# 2. App / LLM config
cp backend/.env.example backend/.env
```

**Pick your LLM provider in `backend/.env`.** The template defaults to Claude:

- **Claude** — just set `ANTHROPIC_API_KEY`. Done.
- **DeepSeek / GLM / other** — set the key **and** repoint the model fields, or
  the agent will call Claude models your key can't serve:

  ```bash
  # DeepSeek
  DEEPSEEK_API_KEY=sk-...
  LLM_PRIMARY_REASONING=deepseek/deepseek-v4-pro
  LLM_PRIMARY_STRONG=deepseek/deepseek-v4-pro
  LLM_PRIMARY_FAST=deepseek/deepseek-v4-flash
  # GLM:  ZAI_API_KEY=...  with  LLM_PRIMARY_*=zai/glm-4.7 , zai/glm-4.5-flash
  ```

Bring up the stack, verify, use:

```bash
docker compose up --build                  # postgres + redis + searxng + backend + frontend
curl http://localhost:8000/api/v1/health   # -> {"status":"ok", ...}
# open http://localhost:3000
docker compose down                        # stop (add -v to also drop db volumes)
```

Optional add-on:

```bash
docker compose --profile embeddings up     # + qdrant (semantic dedup)
```

**Page scraping (Firecrawl)** is not bundled. Point the backend at Firecrawl
Cloud (`SEARCH_FIRECRAWL_USE_SELF_HOSTED=false` + `SEARCH_FIRECRAWL_API_KEY`) or
self-host it and set `SEARCH_FIRECRAWL_SELF_HOSTED_URL`. Without it, page fetch
falls back to jina / httpx.

**In the browser:** type a data question → pick sources from the ranked report →
**Build scrapers** → **Run** → download the dataset. Steer the agent in chat at
any time; login / captcha walls hand control to an embedded browser.

→ How sources are searched and fetched:
[docs/discovery-architecture.md](docs/discovery-architecture.md).

---

## ② Harness CLI (headless / scripting)

Drive a single site without the web UI. **One-time setup** (its own venv):

```bash
cd harness
python -m venv .venv && . .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -c constraints.txt -e .             # -c pins the resolved dependency set
playwright install chromium                     # for JS-rendered sites
# export your LLM key + models in the environment (same vars as backend/.env)
```

Then:

```bash
python -m runtime.cli explore  <site_id>   # explore a site
python -m runtime.cli loop     <site_id>   # explore -> validate loop
python -m runtime.cli validate <site_id>
python -m runtime.cli run      <site_id>   # full production crawl
python -m runtime.cli crawl    <site_id>   # agentic crawl (dynamic sites)
python -m runtime.cli data     <site_id>   # build a data product
```

Put a site's `goal.md` + `seed.json` under `harness/inputs/<site_id>/` (see
`harness/inputs/example/`). Outputs land in `harness/workspaces/<site_id>/`.
More: [harness/README.md](harness/README.md).

---

## ③ Claude Code skill (zero infra)

If you already use Claude Code, copy the skill in and just ask:

```bash
cp -r skills/find-and-scrape-data <your-project>/.claude/skills/
pip install -r skills/find-and-scrape-data/scripts/requirements.txt   # optional helpers
```

Then in Claude Code: *"find data sources for `<topic>`"* or *"scrape `<url>` and
build me a scraper"*. It runs with no backend, no Firecrawl / SearXNG, and no
separate keys — using your own Claude. Full guide:
[skills/find-and-scrape-data/README.md](skills/find-and-scrape-data/README.md).

---

## Architecture

```
                 frontend (Next.js)
                        │  SSE / REST
                        ▼
   backend (FastAPI) ───┬── discovery pipeline (LangGraph + agentic super-node)
                        ├── judging / ranking
                        ├── source adapters  +  search (SearXNG, …)
                        └── harness orchestrator ──► harness (Claude Agent SDK)
                                                     explore→validate→run→crawl→data

   infra: postgres (sessions/events) · redis (cache/limits) · searxng (search)
          · qdrant (optional, dedup) · firecrawl (optional, bring-your-own)
```

The backend's discovery pipeline finds + ranks sources; the **discovery→harness
bridge** (`scripts/discovery_to_harness.py`) stages chosen sources into the
harness, which builds, validates, and runs the scraper. Full design:
[docs/DATASOURCE_DISCOVERY_AGENT_DOC.md](docs/DATASOURCE_DISCOVERY_AGENT_DOC.md),
[docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md](docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md).

## Repository layout

```
backend/                  FastAPI service
  src/agents/             discovery pipeline: LangGraph nodes + agentic super-node
  src/judging/            multi-dimensional source scoring + veto
  src/adapters/           source registries (academic/datasets/government/code/geo)
  src/classifiers/        URL / type / portal classification
  src/tools/              search (searxng/brave/tavily/exa) + scraping + validation
  src/services/           orchestration, event store, run registry, CDP bridge, …
  src/api/                routes + middleware + SSE
  src/db/ , src/models/   persistence + schemas
  tests/                  unit tests
frontend/                 Next.js conversational UI  (src/{app,components,hooks,state})
harness/                  Claude Agent SDK runtime
  runtime/                explore / validate / run / crawl / data agents + CLI
  mcp_server/             browser / workspace / vision tools
  .claude/ domain_skills/ agent config + cross-site skills
skills/                   find-and-scrape-data — Claude Code skill (form ③)
scripts/                  discovery → harness bridge
searxng/                  self-hosted SearXNG config
docs/                     architecture docs
docker-compose.yml        the self-hosted stack
.env.example, backend/.env.example   config templates (copy to .env)
```

## Development

```bash
# backend test suite
cd backend && pip install -c constraints.txt -e ".[dev]" && pytest
# harness test suite
cd harness  && pip install -c constraints.txt -e .         && pytest
```

## Documentation

- [docs/discovery-architecture.md](docs/discovery-architecture.md) — how sources are searched & fetched
- [docs/DATASOURCE_DISCOVERY_AGENT_DOC.md](docs/DATASOURCE_DISCOVERY_AGENT_DOC.md) — full technical reference
- [docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md](docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md) — architecture design
- [SECURITY.md](SECURITY.md) — deployment & security model
- [harness/README.md](harness/README.md) · [skills/find-and-scrape-data/README.md](skills/find-and-scrape-data/README.md)

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The single most
useful thing you can send is **a site that defeated it**: which site, what you
asked for, and which stage gave up.

## License

[Apache-2.0](LICENSE).
