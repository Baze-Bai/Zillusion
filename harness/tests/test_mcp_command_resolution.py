"""`_load_mcp_servers` command resolution.

The committed .mcp.json pins the dev venv python by absolute (Windows) path. In
the Linux sandbox image that path doesn't exist, so the browser-harness MCP
server silently fails to launch and its whole tool surface (browser control,
takeover login, send_user_message) disappears. _load_mcp_servers must fall back
to the running interpreter when the configured command isn't an existing file,
while respecting a command path that actually exists.
"""

from __future__ import annotations

import json
import sys

from runtime.options import _load_mcp_servers


def _write_mcp(tmp_path, command):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "browser-harness": {"command": command, "args": ["-m", "mcp_server.server"]}
            }
        }),
        encoding="utf-8",
    )


def test_missing_command_falls_back_to_current_interpreter(tmp_path):
    _write_mcp(tmp_path, "E:\\Zillusion_v1\\does-not-exist\\.venv\\Scripts\\python.exe")
    out = _load_mcp_servers(tmp_path)
    assert out["browser-harness"]["command"] == sys.executable
    # args + injected env are untouched
    assert out["browser-harness"]["args"] == ["-m", "mcp_server.server"]
    assert out["browser-harness"]["env"]["CRAWLER_EXPLORER_ROOT"] == str(tmp_path)


def test_bare_name_command_normalized(tmp_path):
    # A bare "python" isn't a file path either → normalized to the real interpreter.
    _write_mcp(tmp_path, "python")
    out = _load_mcp_servers(tmp_path)
    assert out["browser-harness"]["command"] == sys.executable


def test_existing_command_path_is_respected(tmp_path):
    # An intentional override that actually exists must win (dev venv python).
    _write_mcp(tmp_path, sys.executable)
    out = _load_mcp_servers(tmp_path)
    assert out["browser-harness"]["command"] == sys.executable
