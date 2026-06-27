"""Stage-3 explore-side wiring for the `api` workflow type (no LLM).

- build_options(disallowed_tools=...) lands in ClaudeAgentOptions
- input_workflow_type flips on inputs/<site>/api_spec.json
- the browser disallow list names real tools on the real server key
"""

from __future__ import annotations

import json

from runtime.options import INTERACTIVE_TOOLS_UNAVAILABLE, build_options
from runtime.run import BROWSER_TOOLS_DISALLOWED_FOR_API, input_workflow_type


def test_build_options_passes_disallowed_tools(tmp_path):
    built = build_options(
        project_root=tmp_path,
        disallowed_tools=BROWSER_TOOLS_DISALLOWED_FOR_API,
        load_mcp_json=False,
        setting_sources=[],
    )
    # The caller's isolation list is preserved AND the always-on interactive
    # built-ins are prepended (deduped, order-preserving).
    assert built.options.disallowed_tools == [
        *INTERACTIVE_TOOLS_UNAVAILABLE,
        *BROWSER_TOOLS_DISALLOWED_FOR_API,
    ]


def test_build_options_default_disallows_interactive_builtins(tmp_path):
    built = build_options(project_root=tmp_path, load_mcp_json=False, setting_sources=[])
    # No caller isolation list, but the interactive built-ins (e.g.
    # AskUserQuestion) are always dropped — they have no answer channel here.
    assert built.options.disallowed_tools == INTERACTIVE_TOOLS_UNAVAILABLE


def test_input_workflow_type_detection(tmp_path):
    site = "api-site-x"
    assert input_workflow_type(site, tmp_path) == "default"
    inp = tmp_path / "inputs" / site
    inp.mkdir(parents=True)
    (inp / "api_spec.json").write_text("{}", encoding="utf-8")
    assert input_workflow_type(site, tmp_path) == "api"


def test_disallow_list_matches_registered_browser_tools():
    """Every name in the disallow list must be a REAL tool on the server under
    the REAL .mcp.json server key — catches drift when browser tools are
    added/renamed (a missed one would silently stay enabled for api runs)."""
    from pathlib import Path

    import mcp_server.server as server

    harness_root = Path(server.__file__).resolve().parent.parent
    mcp_cfg = json.loads((harness_root / ".mcp.json").read_text(encoding="utf-8"))
    (server_key,) = mcp_cfg["mcpServers"].keys()

    browser_attrs = {n for n in dir(server) if n.startswith("browser_")}
    for full in BROWSER_TOOLS_DISALLOWED_FOR_API:
        prefix, _, tool = full.rpartition("__")
        assert prefix == f"mcp__{server_key}", full
        assert tool in browser_attrs, f"disallow list names unknown tool {tool}"
    # ...and the list covers ALL browser_* tools (nothing slipped through).
    listed = {f.rpartition("__")[2] for f in BROWSER_TOOLS_DISALLOWED_FOR_API}
    assert listed == browser_attrs
