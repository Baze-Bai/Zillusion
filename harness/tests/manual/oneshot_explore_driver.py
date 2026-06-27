r"""End-to-end "whole chain" test: drive the REAL explore agent (Claude Agent
SDK + harness MCP + hypothesis-loop SKILL) against a login-walled site, multi-
turn, so the human-takeover pause/resume actually completes.

Why this exists: `runtime.cli explore` uses the SDK's single-shot `query()`, so
it can't feed a "continue" after the agent opens the login window. The real
interactive path is the `claude` TUI. This driver reproduces that path
programmatically with `ClaudeSDKClient` (multi-turn), so the run can be driven +
captured headlessly while a human logs in.

Flow:
  turn 1  ->  `/explore <site>`  — agent attaches, hits the wall, calls
              browser_request_user_login (a VISIBLE window opens), then STOPS.
  [pause] ->  driver waits for a sentinel file (the orchestrator creates it once
              the human has logged in in that window).
  turn 2  ->  "I've logged in, continue" — agent calls browser_save_auth,
              re-fetches the now-reachable page, extracts the goal fields.

Run (project venv, with the LiteLLM proxy already up on :4000):
    .venv\Scripts\python.exe tests\manual\oneshot_explore_driver.py \
        --site gh-account-walled --model deepseek-v4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[2]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

SCRATCH = Path(r"D:\Zillusion_work\login_takeover_test")


def _log_open(site: str):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    f = (SCRATCH / f"oneshot_{site}.transcript.txt").open("w", encoding="utf-8")
    return f


def _emit(f, line: str) -> None:
    print(line, flush=True)
    f.write(line + "\n")
    f.flush()


async def _consume_turn(client, f, *, label: str, budget_s: float) -> dict:
    """Stream one response turn; log text/tool-use/tool-result. Return what
    happened (did the agent open a login window? did it save auth? final text)."""
    from claude_agent_sdk import (
        AssistantMessage, ResultMessage, SystemMessage, TextBlock,
        ToolResultBlock, ToolUseBlock, UserMessage,
    )

    state = {"login_opened": False, "saved_auth": False, "tools": [], "final_text": "", "cost": None}
    start = time.monotonic()
    _emit(f, f"\n========== {label}: streaming ==========")
    async for msg in client.receive_response():
        if time.monotonic() - start > budget_s:
            _emit(f, f"[{label}] wall-clock budget {budget_s}s exceeded — stopping consume")
            break
        if isinstance(msg, SystemMessage):
            continue
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    state["final_text"] = b.text.strip()
                    _emit(f, f"[assistant] {b.text.strip()}")
                elif isinstance(b, ToolUseBlock):
                    state["tools"].append(b.name)
                    short = json.dumps(b.input, ensure_ascii=False, default=str)
                    if len(short) > 300:
                        short = short[:297] + "..."
                    _emit(f, f"[tool] {b.name} {short}")
        elif isinstance(msg, UserMessage):
            for b in (msg.content if isinstance(msg.content, list) else []):
                if isinstance(b, ToolResultBlock):
                    content = b.content
                    if isinstance(content, list):
                        content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    text = str(content)
                    low = text.lower().replace(" ", "")  # normalize: compact vs spaced JSON
                    if '"opened":true' in low or "'opened':true" in low:
                        state["login_opened"] = True
                    if '"saved":true' in low or "'saved':true" in low:
                        state["saved_auth"] = True
                    head = text[:300].replace("\n", " ")
                    _emit(f, f"[result] {head}")
        elif isinstance(msg, ResultMessage):
            state["cost"] = getattr(msg, "total_cost_usd", None)
            _emit(f, f"[{label} done] cost=${state['cost']} stop={getattr(msg,'stop_reason',None)}")
    # Fallback: infer login window from the tool name even if result parse missed.
    if any(t.endswith("browser_request_user_login") for t in state["tools"]):
        state["login_opened"] = True
    return state


async def run(site: str, model: str, login_timeout: int, max_turns: int, turn_budget: int) -> int:
    from claude_agent_sdk import ClaudeSDKClient
    from runtime.options import build_options

    f = _log_open(site)
    sentinel = SCRATCH / f"{site}_logged_in.flag"
    if sentinel.exists():
        sentinel.unlink()

    built = build_options(
        project_root=HARNESS_ROOT,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        model=model,
    )
    _emit(f, f"[setup] site={site} model={model} mcp_servers={built.mcp_servers_count} "
             f"base_url={os.environ.get('ANTHROPIC_BASE_URL')}")

    async with ClaudeSDKClient(options=built.options) as client:
        # --- turn 1: run /explore until the agent pauses at the wall ---------
        await client.query(f"/explore {site}")
        t1 = await _consume_turn(client, f, label="turn1 (/explore -> wall)", budget_s=turn_budget)

        if not t1["login_opened"]:
            _emit(f, "\n[RESULT] turn 1 ended WITHOUT opening a login window.")
            _emit(f, f"  tools used: {sorted(set(t1['tools']))}")
            _emit(f, "  -> the agent did not escalate to human takeover; see transcript above.")
            return 2

        # --- pause: wait for the human to log in in the opened window --------
        _emit(f, "\n" + "!" * 72)
        _emit(f, "  ACTION NEEDED: a visible login window is open. Log in there.")
        _emit(f, f"  Driver is waiting for sentinel: {sentinel}")
        _emit(f, f"  (orchestrator creates it once you confirm login; up to {login_timeout}s)")
        _emit(f, "!" * 72)
        waited = 0
        while waited < login_timeout and not sentinel.exists():
            await asyncio.sleep(3)
            waited += 3
        if not sentinel.exists():
            _emit(f, "[RESULT] timed out waiting for login sentinel; aborting before save_auth.")
            return 3
        _emit(f, f"[resume] sentinel seen after ~{waited}s — sending continue.")

        # --- turn 2: resume -> save_auth + re-fetch + extract ----------------
        await client.query(
            "我已经在弹出的窗口里登录好了。请继续:先调用 browser_save_auth 持久化登录会话,"
            "然后重新访问目标页面,确认现在能看到数据,并抓取 goal.md 要求的字段;完成后简述结果。"
        )
        t2 = await _consume_turn(client, f, label="turn2 (resume -> extract)", budget_s=turn_budget)

    _emit(f, "\n" + "=" * 72)
    _emit(f, "[SUMMARY]")
    _emit(f, f"  turn1 opened login window : {t1['login_opened']}")
    _emit(f, f"  turn2 saved auth          : {t2['saved_auth']}")
    _emit(f, f"  turn1 tools: {sorted(set(t1['tools']))}")
    _emit(f, f"  turn2 tools: {sorted(set(t2['tools']))}")
    _emit(f, f"  transcript: {SCRATCH / f'oneshot_{site}.transcript.txt'}")
    f.close()
    return 0 if (t1["login_opened"] and t2["saved_auth"]) else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-turn end-to-end explore + human-takeover test")
    ap.add_argument("--site", required=True)
    ap.add_argument("--model", default="deepseek-v4")
    ap.add_argument("--proxy", default="http://localhost:4000")
    ap.add_argument("--login-timeout", type=int, default=600,
                    help="seconds to wait for the human login sentinel")
    ap.add_argument("--turn-budget", type=int, default=1800,
                    help="wall-clock safety cap per agent turn (deepseek+thinking can be slow)")
    ap.add_argument("--max-turns", type=int, default=60)
    args = ap.parse_args()

    # Route the SDK/CLI at the LiteLLM proxy (which holds the real DeepSeek key).
    os.environ["ANTHROPIC_BASE_URL"] = args.proxy
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-anything")
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    os.environ.pop("CRAWLER_EXPLORER_ACTIVE_SITE", None)  # keep interactive (not autonomous)

    return asyncio.run(run(args.site, args.model, args.login_timeout, args.max_turns, args.turn_budget))


if __name__ == "__main__":
    raise SystemExit(main())
