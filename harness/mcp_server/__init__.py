"""MCP server bundle for the browser-harness variant.

This server is CDP-first: the Python player (`browser_player`) and
`browser_cdp_send` are the primary interface, with Playwright high-level
sugar on the side. The agent runtime is Claude Code (or claw).
"""

from mcp_server.browser import Browser
from mcp_server.skill_library import SkillEntry, SkillLibrary
from mcp_server.workspace import Memory, Workspace

__all__ = ["Browser", "Memory", "SkillEntry", "SkillLibrary", "Workspace"]
