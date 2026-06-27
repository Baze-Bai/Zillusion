# Phase 0 Spike Findings — Claude Agent SDK on DeepSeek

**Date**: 2026-05-11
**SDK**: `claude-agent-sdk==0.1.81` (Python) + `@anthropic-ai/claude-code@2.1.139` (Node CLI)
**LLM**: DeepSeek v4 (pro + flash) via Anthropic-compatible endpoint at `https://api.deepseek.com/anthropic`

## TL;DR

Claude Agent SDK works against DeepSeek's Anthropic-compatible endpoint **out of the box**. All 3 spikes passed without any patching. We can proceed with full Phase 1-5 build.

## Architecture confirmed

```
backend Python process
       │
       ▼
claude_agent_sdk.query()   ← thin Python wrapper
       │  subprocess
       ▼
claude CLI (Node.js 24)    ← the actual agent harness
       │  HTTPS
       ▼
api.deepseek.com/anthropic ← Anthropic-compatible endpoint (auth via DEEPSEEK_API_KEY)
       │
       ▼
deepseek-v4-pro / deepseek-v4-flash
```

Key: `ClaudeAgentOptions.env` routes env vars to the CLI subprocess. We set
`ANTHROPIC_API_KEY=<DEEPSEEK_KEY>` + `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`
+ `model="deepseek-v4-flash"` — works.

## Spike results

| Spike | Description | Messages | Wall | Cost | Status |
|---|---|---|---|---|---|
| #1 minimal | No tools, prompt: "reply 5 words" | 4 | 6.4s | $0.011 | ✅ |
| #2 custom tool | MCP-style in-process tool `get_query_id` | 7 | 4.2s | $0.126 | ✅ |
| #3 skill IO | Read + Edit a SKILL.md file on disk | 10 | 20.8s | $0.031 | ✅ |

**Tool calls happened correctly**:
- Spike #2: agent called `mcp__spike__get_query_id` and used its return value
- Spike #3: agent called `Read` to inspect file, then `Edit` to append new section; the file on disk shows the change

## Message stream — for SSE protocol design

Observed message classes (all from `claude_agent_sdk`):

| Class | When emitted | Useful fields |
|---|---|---|
| `SystemMessage` | Session init | `subtype="init"`, `data.session_id`, `data.cwd` |
| `AssistantMessage` w/ `ThinkingBlock` | Model thinking pass | (no useful content for SSE) |
| `AssistantMessage` w/ `TextBlock` | Model speaks | `block.text` — surface to user |
| `AssistantMessage` w/ `ToolUseBlock` | Model calls a tool | `block.name`, `block.input` — emit as "tool_started" |
| `UserMessage` w/ `ToolResultBlock` | Tool returns | tie to ToolUseBlock via `tool_use_id` — emit as "tool_completed" |
| `ResultMessage` | Query end | `result`, `total_cost_usd`, `duration_ms`, `num_turns`, `stop_reason`, `is_error` |

**Proposed SSE event mapping** (for Phase 4+):

```
SystemMessage(init)                → sse "agent_started" {session_id, cwd}
AssistantMessage(ThinkingBlock)    → skip (or emit "thinking" with a tick mark)
AssistantMessage(TextBlock)        → sse "agent_text" {text}
AssistantMessage(ToolUseBlock)     → sse "tool_started" {name, input, tool_use_id}
UserMessage(ToolResultBlock)       → sse "tool_completed" {tool_use_id, content}
ResultMessage                      → sse "done" {result, cost, duration, turns}
```

## Cost / latency profile (preliminary)

For the agentic super-node replacing 9 LangGraph nodes:
- 8 turns × ~2s/turn = **~16s** baseline per "portal exploration" cycle
- DeepSeek-flash is ~$0.01-0.03 per simple query, ~$0.10+ per multi-tool MCP query
- For a real data-discovery query with ~10 portals × 8 turns + 2-3 reflect loops, expect **2-5 minutes** + **$0.50-2.00** per query

## Gotchas

1. **CLI subprocess required** — `claude` Node binary must be installed (`npm install -g @anthropic-ai/claude-code`). Add to Dockerfile.
2. **Skills auto-loaded from `<cwd>/.claude/skills/`** — for curated seed skills. For runtime-accumulated skills in `agent-workspace/domain-skills/`, must expose via tools (`lookup_skill` / `propose_skill`).
3. **`permission_mode="bypassPermissions"` is REQUIRED for server-side use** — otherwise CLI prompts for tool approval interactively and hangs in a server context.
4. **`tools=[]` disables built-in tools** — explicitly set this in the options when you only want MCP tools, or default Claude Code tools will leak in.
5. **DeepSeek-flash hallucinates instructions** — Spike #1 said "I'll comply with that request. I will reply with five words exactly. Here are five words: hello world test case done." (much more than 5 words). For production we should use deepseek-v4-pro for anything requiring instruction precision; flash for fan-out work where verbosity is OK.

## Phase 1 entry point

We're ready. Phase 1 starts with wrapping the existing capabilities (search/fetch/probe/skill_lib/judge_dims) as MCP-style tools using `@tool` + `create_sdk_mcp_server`.
