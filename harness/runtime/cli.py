"""Thin CLI wrapper around the SDK runner, for local testing and as a
copy-paste starting point for the FastAPI / ARQ integration.

Usage:
    python -m runtime.cli explore <site_id>          # /explore <site_id>
    python -m runtime.cli prompt "say hello"         # arbitrary prompt
    python -m runtime.cli explore example --json     # stream JSON events

Stream behaviour:
    Default      - human-readable, mirrors what `claude` would show
    --json       - one JSON object per line, ready to forward to a queue
    --quiet      - only the final RunSummary as JSON

This is *not* a replacement for `claude` CLI's TUI; it's a non-interactive
batch tool. The TUI version is what humans use for local exploration; this
is what services use to run the same agent unattended.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from runtime import modality
from runtime.run import RunSummary, explore, run_prompt


def _format_event(event: dict[str, Any]) -> str:
    kind = event.get("kind")
    if kind == "assistant_text":
        return event["text"]
    if kind == "tool_use":
        name = event["name"]
        inp = event.get("input")
        # Trim long inputs for readability.
        if isinstance(inp, dict):
            short = {k: (v if len(repr(v)) < 80 else repr(v)[:77] + "...") for k, v in inp.items()}
        else:
            short = inp
        return f"\033[35m[tool]\033[0m {name} {json.dumps(short, default=str)[:240]}"
    if kind == "tool_result":
        content = event.get("content")
        if isinstance(content, list):
            text_parts = [b.get("text", "") for b in content if isinstance(b, dict)]
            content = "\n".join(text_parts)
        head = str(content)[:240]
        err = " (error)" if event.get("is_error") else ""
        return f"\033[33m[result{err}]\033[0m {head}"
    if kind == "result":
        cost = event.get("cost_usd")
        usage = event.get("usage") or {}
        return (
            f"\033[32m[done]\033[0m cost=${cost or 0:.4f} "
            f"in={usage.get('input_tokens')} out={usage.get('output_tokens')}"
        )
    if kind == "error":
        return f"\033[31m[error]\033[0m {event.get('error')}"
    if kind == "system":
        return f"\033[36m[system]\033[0m {event.get('subtype')}"
    return f"[{kind}] {event}"


def _apply_vision_mode(args: argparse.Namespace) -> None:
    """Honor --vision on|off by setting the CRAWLER_EXPLORER_VISION override that
    runtime.modality.resolve_vision reads (and the orchestrator forwards to the
    MCP server). 'auto' leaves it unset → the mode is inferred from --model."""
    mode = getattr(args, "vision", "auto")
    if mode == "on":
        os.environ["CRAWLER_EXPLORER_VISION"] = "1"
    elif mode == "off":
        os.environ["CRAWLER_EXPLORER_VISION"] = "0"


async def _run(args: argparse.Namespace) -> int:
    _apply_vision_mode(args)
    if args.command == "explore-loop":
        return await _run_explore_loop(args)
    if args.command == "validate":
        return await _run_validate(args)
    if args.command == "run":
        return await _run_run_agent(args)
    if args.command == "crawl":
        return await _run_crawl_agent(args)
    if args.command == "data":
        return await _run_data_agent(args)

    summary = RunSummary()

    if args.command == "explore":
        gen = explore(
            args.site_id,
            permission_mode=args.permission_mode,
            max_turns=args.max_turns,
            model=args.model,
            summary=summary,
        )
    else:
        # Gate the vision surface on the raw-prompt path too (it loads the full
        # browser surface): withhold the mode's OTHER vision tool and forward the
        # resolved mode (+ submodel backend) to the MCP server. Mirrors explore().
        gen = run_prompt(
            args.prompt,
            permission_mode=args.permission_mode,
            max_turns=args.max_turns,
            model=args.model,
            summary=summary,
            disallowed_tools=modality.gated_off_tools(args.model) or None,
            extra_env=modality.vision_env(args.model),
        )

    try:
        async for event in gen:
            if args.json:
                print(json.dumps(event, default=str), flush=True)
            elif not args.quiet:
                line = _format_event(event)
                if line:
                    print(line, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"runtime error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.quiet:
        print(json.dumps(summary.to_dict(), default=str, indent=2))

    return 0 if summary.finished else 2


async def _run_explore_loop(args: argparse.Namespace) -> int:
    """Driver for `explore-loop` subcommand — invokes the orchestrator."""
    from runtime.explore_loop import stream_explore_loop

    final_summary: dict[str, Any] | None = None
    try:
        async for ev in stream_explore_loop(
            args.site_id,
            max_iters=args.max_iters,
            max_cost_usd=args.max_cost_usd,
            model=args.model,
        ):
            if ev.get("kind") == "_summary":
                final_summary = ev["summary"]
                continue
            if args.json:
                print(json.dumps(ev, default=str), flush=True)
            elif not args.quiet:
                kind = ev.get("kind", "")
                if kind == "iter_start":
                    print(
                        f"\033[36m[iter {ev['iter_n']} start]\033[0m site={ev['site_id']}",
                        flush=True,
                    )
                elif kind == "iter_done":
                    color = "\033[32m" if ev["verdict"] == "PASS" else "\033[33m"
                    print(
                        f"{color}[iter {ev['iter_n']} done]\033[0m "
                        f"verdict={ev['verdict']} cost=${ev['cost_usd']:.4f} "
                        f"elapsed={ev['elapsed_seconds']:.1f}s cumulative=${ev['cumulative_cost']:.4f}",
                        flush=True,
                    )
                elif kind == "snapshot":
                    if ev.get("path"):
                        print(f"\033[35m[snapshot]\033[0m {ev['path']}", flush=True)
                elif kind == "validation_start":
                    print(f"\033[33m[iter {ev['iter_n']} validating...]\033[0m", flush=True)
                elif kind == "route_resolved":
                    print(
                        f"\033[36m[route]\033[0m "
                        f"{ev.get('route') or 'NONE (unfinished — will iterate)'}",
                        flush=True,
                    )
                elif kind == "inline_check":
                    print(
                        f"\033[33m[inline]\033[0m ok={ev.get('ok')} {str(ev.get('detail'))[:140]}",
                        flush=True,
                    )
                elif kind == "crawl_start":
                    print(
                        f"\033[35m[agentic crawl start]\033[0m site={ev.get('site_id')}",
                        flush=True,
                    )
                elif kind == "crawl_done":
                    print(
                        f"\033[35m[agentic crawl done]\033[0m outcome={ev.get('outcome')} "
                        f"records={ev.get('records')} cost=${ev.get('cost_usd') or 0:.2f}",
                        flush=True,
                    )
                elif kind == "loop_done":
                    color = "\033[32m" if ev["final_verdict"] == "PASS" else "\033[31m"
                    print(
                        f"\n{color}[LOOP DONE]\033[0m "
                        f"verdict={ev['final_verdict']} reason={ev['exit_reason']} "
                        f"iters={ev['iterations_run']} total_cost=${ev['total_cost_usd']:.4f}",
                        flush=True,
                    )
                # explore_event sub-events suppressed at default verbosity to
                # keep the loop log readable; set --json to see all of them.
    except Exception as exc:  # noqa: BLE001
        print(f"explore-loop crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.quiet and final_summary is not None:
        print(json.dumps(final_summary, default=str, indent=2))

    if final_summary and final_summary.get("final_verdict") == "PASS":
        return 0
    return 2


async def _run_validate(args: argparse.Namespace) -> int:
    """Driver for `validate` subcommand — runs the self-contained validator
    SDK agent (runtime.validate) on one site and prints its gate-computed
    verdict line. Same engine the explore-loop uses between iterations, so a
    manual `validate` and a loop-internal validation reach the same verdict."""
    from runtime.validate import ValidationSummary, validate

    summary = ValidationSummary()
    try:
        async for ev in validate(
            args.site_id,
            permission_mode=args.permission_mode,
            max_turns=args.max_turns,
            model=args.model,
            summary=summary,
        ):
            if args.json:
                print(json.dumps(ev, default=str), flush=True)
            elif not args.quiet:
                line = _format_event(ev)
                if line:
                    print(line, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"validate crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        # Standalone validate owns the process lifecycle — close the pooled
        # validator chromium (explore-loop runs do this at loop end).
        try:
            from runtime.validator_browser import shutdown_browser

            await shutdown_browser()
        except Exception:  # noqa: BLE001
            pass

    if args.quiet:
        print(json.dumps(summary.to_dict(), default=str, indent=2))

    return 0 if summary.verdict == "PASS" else 2


async def _run_run_agent(args: argparse.Namespace) -> int:
    """Driver for the `run` subcommand — runs the self-contained Run agent
    (runtime.run_agent) which executes the validated workflow.py at FULL scope,
    monitors the crawl, keeps the data under runs/<run_id>/, and feeds problems
    back to /explore. Prints the gate-computed outcome line."""
    from runtime.run_agent import RunAgentSummary, run_agent

    summary = RunAgentSummary()
    try:
        async for ev in run_agent(
            args.site_id,
            run_id=args.run_id,
            permission_mode=args.permission_mode,
            max_turns=args.max_turns,
            model=args.model,
            crawl_mode=args.crawl_mode,
            wall_clock_cap_s=args.wall_clock_cap,
            stall_timeout_s=args.stall_timeout,
            output_root=args.output_root,
            steer_offset=args.steer_offset,
            summary=summary,
        ):
            if args.json:
                print(json.dumps(ev, default=str), flush=True)
            elif not args.quiet:
                line = _format_event(ev)
                if line:
                    print(line, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"run crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.quiet:
        print(json.dumps(summary.to_dict(), default=str, indent=2))

    return 0 if summary.outcome == "COMPLETE" else 2


async def _run_crawl_agent(args: argparse.Namespace) -> int:
    """Driver for the `crawl` subcommand — the AGENTIC route. Runs the
    self-contained Agentic Crawl agent (runtime.crawl_agent): a persistent SDK
    session that drives the browser, writes batch-extraction code, and commits
    records to runs/<run_id>/ until complete vs the crawl_brief anchor. Used
    when Explore marked the site too dynamic for a deterministic workflow.py.
    Prints the gate-computed outcome line."""
    from runtime.crawl_agent import crawl_agent
    from runtime.run_agent import RunAgentSummary

    summary = RunAgentSummary()
    try:
        async for ev in crawl_agent(
            args.site_id,
            run_id=args.run_id,
            permission_mode=args.permission_mode,
            max_turns=args.max_turns,
            model=args.model,
            wall_clock_cap_s=args.wall_clock_cap,
            stall_warn_s=args.stall_warn,
            stall_kill_s=args.stall_kill,
            steer_offset=args.steer_offset,
            summary=summary,
        ):
            if args.json:
                print(json.dumps(ev, default=str), flush=True)
            elif not args.quiet:
                line = _format_event(ev)
                if line:
                    print(line, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"crawl crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.quiet:
        print(json.dumps(summary.to_dict(), default=str, indent=2))

    return 0 if summary.outcome == "COMPLETE" else 2


async def _run_data_agent(args: argparse.Namespace) -> int:
    """Driver for the `data` subcommand — runs the self-contained Data Agent
    (runtime.data_agent), a full-capability claude_code session that cleans one
    or more crawled datasets and builds data products under products/<id>/.
    Prints the gate-computed outcome line."""
    from runtime.data_agent import DataAgentSummary, data_agent

    summary = DataAgentSummary()
    try:
        async for ev in data_agent(
            args.sources,
            args.task,
            product_id=args.product_id,
            clean_spec=args.clean_spec,
            permission_mode=args.permission_mode,
            max_turns=args.max_turns,
            model=args.model,
            output_root=args.output_root,
            sample_rows=args.sample_rows,
            top_n=args.top_n,
            max_field_chars=args.max_field_chars,
            summary=summary,
        ):
            if args.json:
                print(json.dumps(ev, default=str), flush=True)
            elif not args.quiet:
                line = _format_event(ev)
                if line:
                    print(line, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"data crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.quiet:
        print(json.dumps(summary.to_dict(), default=str, indent=2))

    return 0 if summary.outcome == "COMPLETE" else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="runtime.cli",
        description="Run the crawler-exploration agent via Claude Agent SDK.",
    )
    p.add_argument(
        "--permission-mode",
        default="bypassPermissions",
        choices=["default", "acceptEdits", "bypassPermissions", "plan"],
    )
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument(
        "--model", default=None, help="Model id (e.g. claude-sonnet-4-6). Defaults to SDK default."
    )
    p.add_argument(
        "--vision",
        choices=["auto", "on", "off"],
        default="auto",
        help="Base-model MODE. 'on' = multimodal (browser_screenshot enabled — the "
        "agent can read page screenshots); 'off' = text-only (screenshot tool "
        "withheld); 'auto' (default) = infer from --model. Overrides the inferred "
        "mode by setting CRAWLER_EXPLORER_VISION.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON event per line (suitable for queue forwarding).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress event stream; print only final RunSummary as JSON.",
    )

    sub = p.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("explore", help="Run /explore <site_id> for one site.")
    ex.add_argument("site_id")

    pr = sub.add_parser("prompt", help="Run an arbitrary prompt.")
    pr.add_argument("prompt", help="Prompt string; supports /slash-command expansion.")

    loop = sub.add_parser(
        "explore-loop",
        help="Reactive loop: /explore <-> validation-agent until PASS or budget hits.",
    )
    loop.add_argument("site_id")
    loop.add_argument(
        "--max-iters", type=int, default=5, help="Hard cap on iterations (default 5)."
    )
    loop.add_argument(
        "--max-cost-usd",
        type=float,
        default=20.0,
        help="Cumulative LLM cost cap in USD (default 20.0).",
    )

    val = sub.add_parser(
        "validate",
        help="Run the self-contained validator (runtime.validate) on one site.",
    )
    val.add_argument("site_id")

    run_p = sub.add_parser(
        "run",
        help="Run the validated workflow.py at FULL scope to produce + keep the dataset.",
    )
    run_p.add_argument("site_id")
    run_p.add_argument(
        "--crawl-mode",
        default="full",
        choices=["full", "sample"],
        help="CRAWL_MODE passed to workflow.py (default full).",
    )
    run_p.add_argument(
        "--wall-clock-cap",
        type=float,
        default=3600.0,
        help="Hard cap on total crawl seconds; breach -> kill (default 3600).",
    )
    run_p.add_argument(
        "--stall-timeout",
        type=float,
        default=600.0,
        help="Kill if no stdout for this many seconds (default 600).",
    )
    run_p.add_argument(
        "--output-root",
        default=None,
        help=r"Relocate bulky output+media here (e.g. D:\Zillusion_work); "
        r"control files stay under workspaces/.",
    )
    run_p.add_argument(
        "--run-id",
        default=None,
        help="Pre-assign the run id (default: random run-<hex>). A caller that "
        "passes this knows runs/<run_id>/ up front (e.g. to watch _run_progress.json).",
    )
    run_p.add_argument(
        "--steer-offset",
        type=int,
        default=None,
        help="Byte offset to resume tailing user_run_steering.md from (default: "
        "EOF at session start). A caller that files an operator message and THEN "
        "starts this run passes the pre-append size so the message is delivered.",
    )

    crawl_p = sub.add_parser(
        "crawl",
        help="Agentic route: harvest data with a persistent browser-driving agent (no workflow.py). "
        "Use a large --max-turns (e.g. 300) — agentic loops are long.",
    )
    crawl_p.add_argument("site_id")
    crawl_p.add_argument(
        "--wall-clock-cap",
        type=float,
        default=7200.0,
        help="Hard cap on total crawl seconds; breach -> kill (default 7200).",
    )
    crawl_p.add_argument(
        "--stall-warn",
        type=float,
        default=300.0,
        help="Soft steering warning after this many seconds with no progress (default 300).",
    )
    crawl_p.add_argument(
        "--stall-kill",
        type=float,
        default=900.0,
        help="Kill after this many seconds with no new commits / cursor advance (default 900). "
        "The primary brake (there is no token cap).",
    )
    crawl_p.add_argument(
        "--run-id",
        default=None,
        help="Pre-assign the run id (default: random run-<hex>).",
    )
    crawl_p.add_argument(
        "--steer-offset",
        type=int,
        default=None,
        help="Byte offset to resume tailing user_run_steering.md from (default: EOF at start).",
    )

    data_p = sub.add_parser(
        "data",
        help="Data Agent: clean + build data products from one or more crawled datasets.",
    )
    data_p.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="One or more sources: site_id / site_id:run_id / a file path.",
    )
    data_p.add_argument(
        "--task", required=True, help="What product(s) to build (+ any cleaning instructions)."
    )
    data_p.add_argument(
        "--clean-spec",
        default=None,
        help="Optional path to a file holding the cleaning instructions.",
    )
    data_p.add_argument("--product-id", default=None, help="Reuse / override the product id.")
    data_p.add_argument(
        "--output-root",
        default=None,
        help=r"Relocate bulky clean/products here (e.g. D:\Zillusion_work); "
        r"control files stay under products/.",
    )
    data_p.add_argument(
        "--sample-rows",
        type=int,
        default=20,
        help="profile_dataset sample rows surfaced to the agent (default 20).",
    )
    data_p.add_argument(
        "--top-n", type=int, default=15, help="profile_dataset top values per field (default 15)."
    )
    data_p.add_argument(
        "--max-field-chars",
        type=int,
        default=200,
        help="Truncate long field values in summaries (default 200).",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
