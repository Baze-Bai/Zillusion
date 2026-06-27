"""Claude Agent SDK adapter.

This module replaces the `claude` CLI as the agent runtime so the project can
be embedded in a FastAPI / worker / batch job. The MCP server, skills, hooks,
CLAUDE.md, and slash commands are loaded the same way the CLI loads them.

Public API (imported on demand, not eagerly, so `runtime.slash` and
`runtime.options` are usable without the SDK installed):

    from runtime.options import build_options
    from runtime.slash import expand
    from runtime.run import explore, run_prompt, RunSummary
    python -m runtime.cli ...

The SDK is a soft dependency: any module that needs it raises a friendly
ImportError when missing, but `runtime.slash` (pure-Python) and
`runtime.options` (only needs the options class) work standalone.
"""

__all__: list[str] = []
