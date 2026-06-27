"""Minimal diagnostic: does DeepSeek-V4-Pro via Claude Agent SDK actually
emit Read tool calls when configured with built-in tools?

Hypothesis to test: in e2e runs the main LLM (DeepSeek) said "Let me read
the fetched pages" but never emitted a Read tool_call. Two possibilities:
  (A) The Read tool is registered/forwarded correctly, but the LLM
      chooses not to use it (prompt problem).
  (B) The Read tool is NOT actually reaching the LLM as a callable
      (SDK / LiteLLM proxy / DeepSeek schema mismatch).

This test isolates the question. We give the model ONE job — read a
specific file — and watch the event stream for any Read tool_use block.

  - If Read is emitted → root cause is (A), prompt engineering problem.
  - If Read is NOT emitted → root cause is (B), tool-layer problem.
    Look at the assistant message to see what the model did instead
    (refused / fabricated content / used another tool).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE / ".env")

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions, query,
    AssistantMessage, SystemMessage, UserMessage, ResultMessage,
    TextBlock, ToolUseBlock, ToolResultBlock,
)

from src.agents.agentic.runner import _deepseek_subprocess_env  # noqa: E402


async def main() -> None:
    # Build an isolated workspace with one file the agent must read.
    workspace = (HERE / "agent-workspace" / "_diag_read_tool").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "secret.txt").write_text(
        "MAGIC_VALUE_42_zillusion_diagnostic",
        encoding="utf-8",
    )
    prompt_path = workspace / "system_prompt.md"
    prompt_path.write_text(
        "You are a diagnostic test agent. Your ONE task: call the Read "
        "tool with file_path='secret.txt' to read the file in your "
        "current working directory, then echo the file content back "
        "verbatim in plain text. Do nothing else. Do not refuse. Do not "
        "ask questions. Just call Read('secret.txt') and print what you "
        "got. The file definitely exists and is small.",
        encoding="utf-8",
    )

    options = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(prompt_path)},
        model="deepseek-v4-pro",
        fallback_model="deepseek-v4-flash",
        max_turns=5,
        env=_deepseek_subprocess_env(),
        cwd=str(workspace),
        # No MCP tools registered — only built-in Read.
        tools=["Read"],
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
    )

    print("=" * 72)
    print(f"Workspace: {workspace}")
    print(f"Target file content: 'MAGIC_VALUE_42_zillusion_diagnostic'")
    print(f"Model: deepseek-v4-pro")
    print(f"Allowed tools: ['Read']")
    print("=" * 72)
    print()

    read_calls: list[dict] = []
    other_tool_calls: list[dict] = []
    assistant_texts: list[str] = []
    final_text = ""
    final_cost = 0.0
    n_turns = 0
    saw_magic_in_output = False

    user_prompt = "Read secret.txt and tell me what's in it."

    try:
        async for msg in query(prompt=user_prompt, options=options):
            mt = type(msg).__name__
            if isinstance(msg, SystemMessage):
                print(f"[system] subtype={getattr(msg, 'subtype', '?')}")
                # Init usually carries the resolved tool list — print it
                data = getattr(msg, 'data', None) or {}
                if isinstance(data, dict):
                    tools_resolved = data.get('tools')
                    if tools_resolved is not None:
                        print(f"         resolved_tools = {tools_resolved}")
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        assistant_texts.append(text)
                        print(f"[assistant text] {text[:300]}")
                        if "MAGIC_VALUE_42" in text:
                            saw_magic_in_output = True
                    elif isinstance(block, ToolUseBlock):
                        rec = {"name": block.name, "input": block.input}
                        if block.name == "Read":
                            read_calls.append(rec)
                            print(f"[Read call] input={block.input}")
                        else:
                            other_tool_calls.append(rec)
                            print(f"[other tool] name={block.name} input={block.input}")
            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        preview = str(block.content)[:200].replace("\n", " ")
                        print(f"[tool_result] tool_use_id={block.tool_use_id[:12]} content={preview}")
            elif isinstance(msg, ResultMessage):
                final_text = (msg.result or "")[:500]
                final_cost = float(getattr(msg, "total_cost_usd", 0.0) or 0.0)
                n_turns = int(getattr(msg, "num_turns", 0) or 0)
                if "MAGIC_VALUE_42" in (msg.result or ""):
                    saw_magic_in_output = True
            else:
                print(f"[{mt}]")
    except Exception as e:
        import traceback
        print(f"\nEXCEPTION {type(e).__name__}: {e}")
        traceback.print_exc()

    print()
    print("=" * 72)
    print("DIAGNOSIS")
    print("=" * 72)
    print(f"Turns:                  {n_turns}")
    print(f"Cost:                   ${final_cost:.4f}")
    print(f"Read tool calls:        {len(read_calls)}")
    print(f"Other tool calls:       {len(other_tool_calls)} {[c['name'] for c in other_tool_calls]}")
    print(f"Saw MAGIC_VALUE in out: {saw_magic_in_output}")
    print(f"Final text (first 500): {final_text!r}")
    print()
    if read_calls:
        print("VERDICT: ✅ DeepSeek emitted Read tool call(s).")
        print("         Root cause is prompt engineering, not tool layer.")
        print("         Path A (soft prompt with motivation) is correct.")
    else:
        print("VERDICT: ❌ DeepSeek did NOT emit any Read tool call.")
        print("         Root cause is tool-layer (SDK / proxy / model).")
        print("         Soft prompt won't fix this. Need to either:")
        print("           - Switch to self-implemented read_workspace MCP tool")
        print("           - Or accept the LLM only consumes what's in tool")
        print("             responses directly (no on-demand file reads).")
        if saw_magic_in_output:
            print("         NOTE: MAGIC string appeared in output but no Read")
            print("         call — model likely fabricated or used another route.")


if __name__ == "__main__":
    asyncio.run(main())
