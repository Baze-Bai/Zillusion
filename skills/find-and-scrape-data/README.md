# find-and-scrape-data — a Claude Code skill

Turn a plain-language data need into **either a vetted list of data sources or a
working, validated scraper** — run entirely by *your own* Claude Code, with no
backend, no extra services, and no project API keys.

It's a self-contained distillation of the Zillusion discover → explore → build
pipeline. The deliverable is one of:

- **A ranked list of data sources** for a topic (APIs, files, embedded tables), or
- **A runnable `workflow.py` scraper** for a chosen source, validated against the
  live page, with sample output.

## Install

Copy the folder into your project's (or global) Claude Code skills directory:

```bash
cp -r find-and-scrape-data <your-project>/.claude/skills/
# or, to make it available everywhere:
cp -r find-and-scrape-data ~/.claude/skills/

# optional — the helper scripts' one dependency
pip install -r <your-project>/.claude/skills/find-and-scrape-data/scripts/requirements.txt
```

That's it — Claude Code auto-discovers the skill from `SKILL.md`.

## How to use it — just describe what you want

You don't run a command. The skill activates **automatically** when your request
matches it. Phrasings that trigger it:

- *"find / discover data sources for `<topic>`"*
- *"where can I get data about `<X>`"* · *"what dataset has `<Y>`"*
- *"scrape `<url>` and build me a scraper"*

You can also name it explicitly: *"use find-and-scrape-data to …"*.

It will **not** take over a one-off factual lookup, analysis of data you already
have, or fixing an existing scraper.

## What happens

Claude sets up a workspace dir (`./fsd-<slug>/`), runs a quick capability probe,
then works three phases — narrating as it goes and saving everything to disk:

1. **Discover** — queries free registries (OpenAlex / Hugging Face / CKAN /
   APIs.guru via `scripts/discover_sources.py`) + `WebSearch` + `WebFetch`, and
   classifies each candidate as `api` / `file` / `embedded`.
2. **Explore + route** — probes a chosen source (`scripts/probe.py`) and decides
   how to harvest it (inline / deterministic scraper / hand-back / infeasible).
3. **Build + validate + run** — writes `workflow.py`, validates the output with
   `scripts/run_and_check.py` (record count, required fields, spot-check against
   the live page), then runs it at full scope.

## What you end up with

```
fsd-<slug>/
├── state.json              run ledger (phase, sources, chosen, routes)
├── sources.jsonl           ranked sources found
├── goal.md                 per chosen source — intent + required fields
├── hypotheses.yaml         what probing confirmed
├── samples/                pages actually fetched
├── workflow.py             the generated scraper (deterministic route)
├── selectors.yaml          its manifest
└── runs/<run_id>/output.json   full data
```

Plain files — read them, edit `workflow.py`, re-run `python workflow.py` yourself.

## Requirements

- **Claude Code** (the model is your own — quality scales with the model you run).
- Optional: `httpx` (helper scripts) and `playwright` (only for JS-rendered
  pages; Claude offers to install it when a page genuinely needs it).
- No backend, no Firecrawl/SearXNG, no MCP server, no project API keys.
- Credentials stay inside scripts; for keyed APIs the skill surfaces the signup
  URL instead of self-registering.

## What's weaker than the full Zillusion app (standalone honesty)

- No 23k-API semantic index (replaced by WebSearch + registry APIs).
- No persistent agentic crawler — a too-dynamic site gets a `crawl_brief.md`
  handed back to you instead of a finished scraper.
- No append-only / secret-leak enforcement hooks (Phase 3 still secret-scans).
- A browser may need installing for JS-heavy pages.

If a full `harness/` sits next to your project, the skill detects it and offers
to delegate the heavy crawling to it.

## Under the hood

- `SKILL.md` — the playbook Claude follows (router for the three phases).
- `references/*.md` — the detailed per-phase guides Claude reads on demand.
- `scripts/` — dependency-light helper programs (`discover_sources.py`,
  `probe.py`, `run_and_check.py`); see [`scripts/README.md`](scripts/README.md).
