# harness — Zillusion crawler-explorer harness

**Agent**: Claude Code (or `claw`).
**Browser interface**: Playwright + CDP-first via MCP, with a Python player.
**Learning**: per-site `helpers.py` (append-only) + cross-site `memory/`
+ cross-site `domain_skills/` + project-scoped `.claude/skills/`.

The Claude-Code-driven crawler harness: the agent probes a site through a CDP-first
browser surface, writes the scraper, and accumulates what it learns in a skill
library that carries across sites.

## How it fits together

```
+-----------------------------------------------------------+
| Claude Code  (agent runtime, runs the loop)               |
|   reads CLAUDE.md, .claude/skills/*, .claude/commands/*   |
|   reads memory/*.md and skill_list at run start           |
|   issues tool calls over MCP stdio                        |
+----------------+------------------------------------------+
                 |  MCP (stdio)
                 v
+-----------------------------------------------------------+
| browser-harness MCP server  (mcp_server/server.py)        |
|   browser_attach / goto / evaluate / content              |
|   browser_cdp_send  (first-class, not escape hatch)       |
|   browser_player    (async Python with page/context/cdp)  |
|   browser_snapshot                                        |
|   workspace_*  (per-site, helpers.py append-only)         |
|   memory_*     (cross-site prose)                         |
|   skill_*      (cross-site structured library)            |
+----------------+------------------------------------------+
                 |  Playwright async API + CDP
                 v
            Chromium  (managed by the MCP process)
```

## Layout

```
harness/
  .mcp.json
  CLAUDE.md
  .claude/
    commands/
      explore.md
    skills/                      (run `ls .claude/skills` for the current set)
      hypothesis-loop/SKILL.md   how to run the loop
      browser-probe/SKILL.md     CDP-first probe patterns
      api-probe/SKILL.md         probing an HTTP API, browserless
      validate-workflow/SKILL.md run + grade workflow.py before DONE
      agentic-crawl/SKILL.md     the no-script harvesting route
      data-product/SKILL.md      cleaning + building products from a dataset
      skill-curator/SKILL.md     when to promote / prune
  mcp_server/
    __init__.py
    browser.py                   Playwright + CDP + Python player
    workspace.py                 workspace + memory
    skill_library.py             cross-site skill storage
    server.py                    FastMCP stdio entry
  domain_skills/
    README.md
    dismiss-cookie-banner-eu/    seed skill
  inputs/example/                HN scaffold
  memory/                        cross-site prose
  workspaces/                    <site_id>/ on first run
```

## What this variant adds over the Playwright one

| Capability | Playwright variant | Harness variant |
| --- | --- | --- |
| `browser_player` (async Python) | no | yes |
| `helpers.py` append-only | no | yes |
| Cross-site skill library | no | yes (`skill_*` tools + `domain_skills/`) |
| Cross-site memory | yes | yes |
| `.claude/skills/` (project-scoped) | the crawl skills | **same**, plus `skill-curator` |
| Bundled seed skill | no | yes (`dismiss-cookie-banner-eu`) |

## Setup

```powershell
# from this folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -c constraints.txt -e .
playwright install chromium
```

Then point Claude Code or `claw` at this directory:

```powershell
claude
# or
claw prompt "/explore example"

# or programmatic / cloud (Claude Agent SDK, non-interactive)
python -m runtime.cli explore example
python -m runtime.cli --json explore example     # one JSON event per line
                                                 # (--json/--quiet/--model/--max-turns/
                                                 #  --vision/--permission-mode are GLOBAL:
                                                 #  they go BEFORE the subcommand)
```

The `runtime/` package replaces the `claude` CLI for cloud deployments
without touching anything else. See [Cloud deployment](#cloud-deployment).

## MCP tools (one-line cheat sheet)

### Browser
- `browser_attach(site_id)` -> bind workspace + start Chromium
- `browser_goto`, `browser_evaluate`, `browser_content`
- `browser_cdp_send(method, params)` -> raw CDP, first-class
- `browser_player(script)` -> async Python with `page`, `context`, `cdp`
- `browser_snapshot(name)` -> HTML + PNG into samples/

### Workspace
- `workspace_init`, `workspace_read`, `workspace_write`
- `workspace_append_log`, `workspace_append_facts`
- `workspace_helper_append(name, code)` -> append to helpers.py
- `workspace_list_samples`

### Memory (cross-site prose)
- `memory_index`, `memory_read`, `memory_append`

### Skills (cross-site structured)
- `skill_list`, `skill_read`
- `skill_propose(skill_id, title, when_to_use, description, evidence?, recipe?)`
- `skill_record_use(skill_id, success)`

## Learning loop in one paragraph

The agent reads `memory/*` and `skill_list` at run start. Probes happen
through `browser_*` tools; durable per-site helpers go in `helpers.py` via
`workspace_helper_append` (append-only). Mid-run, when the agent finds a
*transferable* technique it proposes a skill with `skill_propose`; each
later run that applies the skill calls `skill_record_use` to bump the
counter. Free-form observations the agent isn't ready to formalise go to
`memory_append`. The `skill-curator` skill explains when each layer is
appropriate and how to prune false generalisations.

This is the same accumulation pattern as the previous design conversation
laid out, now wired so Claude Code (rather than a custom Python runner)
drives it.

## Cloud deployment

`claude` is a terminal-UI app — not suitable for being driven from a
backend service. For cloud / multi-tenant deployments swap the agent
runtime to **Claude Agent SDK**, the same agent loop packaged as a Python
library. The MCP server (with every tool including `browser_player` and
`skill_*`), skills, hooks, CLAUDE.md, `domain_skills/`, `memory/`,
`inputs/`, `workspaces/` are **all unchanged**.

### Minimal CLI

```powershell
python -m runtime.cli explore example                   # human-readable
python -m runtime.cli --json explore example            # JSON event per line
python -m runtime.cli prompt "/show-state example"      # arbitrary prompt
python -m runtime.cli --quiet --max-turns 2 prompt "say hello"
```

Inside `runtime/`:

| Module | Role |
| --- | --- |
| `runtime.options.build_options(...)` | reads `.mcp.json` + `.claude/settings.json` + `CLAUDE.md`; returns `ClaudeAgentOptions` |
| `runtime.slash.expand(prompt, root)` | converts `/explore example` into the matching command body; pure-Python fallback |
| `runtime.run.explore(site_id, ...)` | async generator: yields normalised events |
| `runtime.run.run_prompt(prompt, ...)` | same, but for any prompt (slash commands auto-expanded) |
| `runtime.run.RunSummary` | aggregate: `turn_count`, `tool_calls`, `tool_call_breakdown`, `total_cost_usd`, `total_input_tokens`, `total_output_tokens` |
| `runtime.cli.main()` | `python -m runtime.cli` entry |

### Minimal FastAPI sketch

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import json
from runtime.run import explore, RunSummary

app = FastAPI()

@app.post("/api/explore/{site_id}")
async def stream_explore(site_id: str):
    async def gen():
        summary = RunSummary()
        async for evt in explore(site_id, summary=summary):
            yield f"data: {json.dumps(evt, default=str)}\n\n"
        yield f"event: done\ndata: {json.dumps(summary.to_dict())}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
```

For multi-tenant isolation, give each job a worktree
(`cp -r template/ /jobs/<job_id>/`). The skill library
(`domain_skills/`) is a deliberate decision:

| Strategy | Trade-off |
| --- | --- |
| Shared `domain_skills/` across tenants | cross-pollination ↑, novel patterns spread faster; minor leak risk (a `when_to_use` could leak the site class it was discovered on) |
| Per-tenant `domain_skills/` | safe but each tenant pays the "first time learning" cost again; consider seeding new tenants with a curated subset |
| Hybrid (recommended) | `domain_skills/global/` (human-curated, shipped) + `domain_skills/<tenant>/` (auto-grown) — prevents prompt-injection-style cross-tenant pollution |

### Programmatic API

```python
from runtime.run import explore, RunSummary
from runtime.options import build_options

# 1. As async generator (FastAPI / worker):
summary = RunSummary()
async for evt in explore("xhs-tokyo", summary=summary):
    if evt["kind"] == "tool_use":
        meter_token_usage(...)  # custom telemetry
    forward_to_sse(evt)

# 2. Build options yourself, then call sdk.query() directly for full control:
from claude_agent_sdk import query
built = build_options(project_root="/srv/jobs/abc123")
async for msg in query(prompt="/explore xhs-tokyo", options=built.options):
    ...
```

### What stays vs what changes

| Component | CLI deployment | Cloud (SDK) deployment |
| --- | --- | --- |
| `mcp_server/` | stdio subprocess | **same**, stdio subprocess |
| every MCP tool (including `browser_player`, `skill_*`) | exposed | **same**, exposed identically |
| `.claude/skills/` | loaded by CLI | loaded by SDK via `setting_sources=["project"]` |
| `.claude/settings.json` hooks (guard + memory inject) | run by CLI | run by SDK |
| `domain_skills/` library | accessed by `skill_*` MCP tools | **same**, no change |
| `CLAUDE.md` | injected by CLI | injected by the `claude_code` system prompt preset |
| Slash commands | expanded by CLI | expanded by SDK (or `runtime.slash` fallback) |
| Agent loop driver | `claude` binary | `claude-agent-sdk` library |
| Permission prompts | interactive | `permission_mode="bypassPermissions"` or `can_use_tool` callback |

### Caveats

- `claude-agent-sdk` is a real dependency (in `pyproject.toml`). `runtime.slash`
  works without it; `runtime.options` and `runtime.run` need it.
- `ClaudeAgentOptions.settings` is a path string, not a dict. We use
  `setting_sources=["project"]` instead.
- `ANTHROPIC_API_KEY` must be set; runtime fails fast with a clear message.
- Per-job worktree isolation is required for multi-tenant. Sharing
  `workspaces/` will cross-contaminate exploration logs.
- `domain_skills/` cross-tenant sharing policy is a product decision; see
  the trade-off table above.
