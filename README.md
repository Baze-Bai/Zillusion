# Zillusion — Data Source Discovery & Scraper-Building Agent

Turn a plain-language data need into **either a vetted list of data sources or a
working, validated scraper that produces a downloadable dataset** — driven by an
LLM agent end to end.

You ask *"where can I get data about X?"* → the agent discovers and ranks
sources (APIs, files, embedded tables) → for any source you pick, it explores
the site, **writes a runnable scraper**, validates it against the live page,
runs it at full scale, and hands you the data.

> ⚠️ **Security model:** this ships configured for **single-tenant, localhost
> use** (no built-in user accounts). Before exposing it on a network, read
> [SECURITY.md](SECURITY.md).

---

## Three ways to use it

| Form | For whom | What you run | Weight |
|------|----------|--------------|--------|
| **① Self-hosted web app** | Most users — full experience | `docker compose up`, browse `localhost:3000` | Heavy (full stack) |
| **② Harness CLI** | Developers, scripting, CI | `python -m runtime.cli <cmd> <site>` | Medium |
| **③ Claude Code skill** | Anyone already using Claude Code | drop `skills/find-and-scrape-data` into `.claude/skills/` | Light (near zero infra) |

All three share the same harness core. **You always bring your own LLM API key**
(DeepSeek / GLM / Claude / OpenAI / …) — none is bundled.

---

## ① Self-hosted web app (quickstart)

**Requirements:** Docker + Docker Compose, and at least one LLM provider API key.

```bash
git clone <your-repo-url> zillusion && cd zillusion

# 1. Compose-level config
cp .env.example .env
#    set SEARXNG_SECRET (openssl rand -hex 32) in .env

# 2. App / LLM config — put your provider key(s) here
cp backend/.env.example backend/.env
#    fill ANTHROPIC_API_KEY / DEEPSEEK_API_KEY / ZAI_API_KEY / ... as needed

# 3. Bring up the core stack (postgres + redis + searxng + backend + frontend)
docker compose up --build

# open http://localhost:3000
```

Optional add-ons:

```bash
docker compose --profile embeddings up   # + qdrant (semantic dedup)
```

**Page scraping (Firecrawl)** is not bundled. Either point the backend at
Firecrawl Cloud (`SEARCH_FIRECRAWL_USE_SELF_HOSTED=false` +
`SEARCH_FIRECRAWL_API_KEY` in `backend/.env`) or run Firecrawl yourself and set
`SEARCH_FIRECRAWL_SELF_HOSTED_URL`.

**In the browser:** type a data question → pick sources from the ranked report →
**Build scrapers** → **Run** → download the dataset. You can steer the agent in
the chat at any time; login/captcha walls hand control to an embedded browser.

How sources are searched and fetched (registry APIs + self-hosted SearXNG, no
search keys needed by default): see [docs/discovery-architecture.md](docs/discovery-architecture.md).

---

## ② Harness CLI (headless / scripting)

Drive a single site without the web UI:

```bash
cd harness
python -m runtime.cli explore  <site_id>   # explore a site
python -m runtime.cli loop     <site_id>   # explore -> validate loop
python -m runtime.cli validate <site_id>
python -m runtime.cli run      <site_id>   # full production crawl
python -m runtime.cli crawl    <site_id>   # agentic crawl (dynamic sites)
python -m runtime.cli data     <site_id>   # build a data product
```

Outputs land in `harness/workspaces/<site_id>/` (`workflow.py` + data).
See [harness/README.md](harness/README.md).

---

## ③ Claude Code skill (zero infra)

If you already use Claude Code, copy the skill into your project and just ask:

```bash
cp -r skills/find-and-scrape-data <your-project>/.claude/skills/
pip install -r skills/find-and-scrape-data/scripts/requirements.txt   # optional helpers
```

Then in Claude Code: *"find data sources for <topic>"* or *"scrape <url> and
build me a scraper"*. The skill bundles a Markdown playbook plus small,
dependency-light helper scripts the agent runs directly — no backend, no
Firecrawl/SearXNG, no separate keys.

---

## Repository layout

```
backend/    FastAPI: discovery pipeline + harness orchestration + web API
frontend/   Next.js conversational web UI
harness/    Claude Agent SDK runtime: explore / validate / run / crawl / data
skills/     find-and-scrape-data — Markdown playbook + helper scripts (form ③)
scripts/    discovery → harness bridge
searxng/    SearXNG config
docs/        architecture & model-config notes
```

## License

[Apache-2.0](LICENSE).
