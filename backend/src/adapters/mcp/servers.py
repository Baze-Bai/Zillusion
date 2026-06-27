"""Seed registry of MCP servers and their result parsers.

Hybrid A+C model
================
A — single-tenant tokens
    Tokens come from the existing ``.env`` (already exposed via
    ``settings.adapter.*`` and ``settings.search.*``). Every server below
    is wrapped in a "register only if its token is set" check, so a
    fresh checkout starts with **zero** registered MCP adapters and
    nothing breaks. As soon as the operator drops a key in ``.env``,
    the corresponding adapter shows up at next start.

C — truly free / no-token tier
    The honest assessment is that the MCP ecosystem has very few servers
    that are both free AND useful for *data-source discovery*. Most useful
    servers wrap SaaS/API endpoints that demand tokens (GitHub, Brave,
    Notion, Slack, …). Reference servers without tokens (filesystem,
    memory, time, sequentialthinking, fetch) are agent *tools*, not data
    *registries* — they don't fit the BaseRegistryAdapter contract.

    Community servers like ``wikipedia-mcp`` or ``arxiv-mcp`` do exist
    and would fit; they're left out of the seed list only because their
    tool names / response shapes haven't been pinned in this codebase.
    Add them by appending another ``_build_*`` builder to ``SEED_BUILDERS``.

Each adapter is *lazily connected*: ``register_config`` is called at
import time, but the underlying ``npx``/``uvx`` subprocess only spawns
the first time ``adapter.search()`` runs. That keeps boot fast and
isolates startup from MCP server-startup failures.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.adapters.mcp.adapter import MCPRegistryAdapter
from src.adapters.registry import AdapterRegistry
from src.config import settings
from src.models.data_source import AccessLevel, DataSourceType
from src.services.mcp_client import MCPServerConfig

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Result parser helpers — turn an mcp.types.CallToolResult into list[dict].
#
# Every parser must return dicts with at least ``url``; everything else
# (title, description, tags, …) is best-effort. The adapter's search()
# tolerates missing keys gracefully.
# ──────────────────────────────────────────────────────────────────────
def _join_text_blocks(result: Any) -> str:
    """Concatenate every TextContent block in a CallToolResult into one string.

    Works against both the SDK's typed result (``result.content`` is a list of
    objects with ``.text``) and the loose dict form returned in some unit-test
    mocks.
    """
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content", [])
    pieces: list[str] = []
    for block in (content or []):
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            pieces.append(str(text))
    return "\n".join(pieces)


def _safe_json_loads(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some servers wrap the JSON payload in framing text. Try to recover
        # the first {...} or [...] block.
        for opener, closer in (("{", "}"), ("[", "]")):
            i = text.find(opener)
            j = text.rfind(closer)
            if i != -1 and j != -1 and j > i:
                try:
                    return json.loads(text[i:j + 1])
                except json.JSONDecodeError:
                    continue
        return None


def parse_brave_search(result: Any) -> list[dict]:
    """Brave's MCP server returns one TextContent with a formatted block list.

    The reference implementation prints results as repeating::

        Title: …
        Description: …
        URL: …

    separated by blank lines. We tolerate either ordering and silently skip
    blocks missing a URL.
    """
    text = _join_text_blocks(result)
    items: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        item: dict[str, str] = {}
        for line in block.splitlines():
            stripped = line.strip()
            for prefix, key in (
                ("Title:", "title"),
                ("URL:", "url"),
                ("Description:", "description"),
            ):
                if stripped.startswith(prefix):
                    item[key] = stripped[len(prefix):].strip()
                    break
        if item.get("url"):
            items.append(item)
    return items


def parse_github_search(result: Any) -> list[dict]:
    """GitHub's MCP server returns the GitHub Search API response as JSON text."""
    text = _join_text_blocks(result)
    data = _safe_json_loads(text)
    if data is None:
        return []
    raw_items = (
        data.get("items", [])
        if isinstance(data, dict)
        else (data if isinstance(data, list) else [])
    )
    items: list[dict] = []
    for repo in raw_items:
        if not isinstance(repo, dict):
            continue
        items.append({
            "url": repo.get("html_url") or repo.get("url") or "",
            "title": repo.get("full_name") or repo.get("name") or "",
            "description": repo.get("description") or "",
            "tags": repo.get("topics") or [],
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "language": repo.get("language"),
            "updated_at": repo.get("updated_at"),
            "default_branch": repo.get("default_branch"),
        })
    return items


def parse_tavily_search(result: Any) -> list[dict]:
    """Tavily's MCP server returns ``{"results": [{url, title, content, ...}]}``."""
    text = _join_text_blocks(result)
    data = _safe_json_loads(text)
    if not isinstance(data, dict):
        return []
    items: list[dict] = []
    for r in data.get("results", []):
        if not isinstance(r, dict):
            continue
        items.append({
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "description": (r.get("content") or "")[:500],
            "score": r.get("score"),
        })
    return items


# ──────────────────────────────────────────────────────────────────────
# Server builders — each returns an MCPRegistryAdapter or None when its
# required token is missing. Skipping is silent at INFO level so it
# surfaces clearly in startup logs without blocking boot.
# ──────────────────────────────────────────────────────────────────────
def _build_github_adapter() -> MCPRegistryAdapter | None:
    token = settings.adapter.github_token
    if not token:
        logger.info(
            "MCP[github]: skipped — ADAPTER_GITHUB_TOKEN not set "
            "(get one at https://github.com/settings/tokens)"
        )
        return None
    config = MCPServerConfig(
        name="github",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": token},
    )
    return MCPRegistryAdapter(
        server_config=config,
        search_tool="search_repositories",
        result_parser=parse_github_search,
        query_arg="query",
        extra_args={"perPage": 15},
        display_name="GitHub (MCP)",
        domains=["code", "software", "tech"],
        worker_tags=["github"],
        source_type=DataSourceType.API,
        access_level=AccessLevel.API_KEY_FREE,
        requires_auth=True,
    )


def _build_brave_search_adapter() -> MCPRegistryAdapter | None:
    api_key = settings.search.brave_api_key
    if not api_key:
        logger.info(
            "MCP[brave]: skipped — SEARCH_BRAVE_API_KEY not set "
            "(get one at https://brave.com/search/api/)"
        )
        return None
    config = MCPServerConfig(
        name="brave_search",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-brave-search"],
        env={"BRAVE_API_KEY": api_key},
    )
    return MCPRegistryAdapter(
        server_config=config,
        search_tool="brave_web_search",
        result_parser=parse_brave_search,
        query_arg="query",
        extra_args={"count": 15},
        display_name="Brave Search (MCP)",
        domains=["general", "web"],
        # Tag as kb/api_directory so the router activates it on knowledge-
        # lookup and "find an API" intents but not for every web query
        # (the existing Brave REST adapter handles plain web search).
        worker_tags=["kb", "api_directory"],
        source_type=DataSourceType.API,
        access_level=AccessLevel.API_KEY_FREE,
        requires_auth=True,
    )


def _build_tavily_adapter() -> MCPRegistryAdapter | None:
    api_key = settings.search.tavily_api_key
    if not api_key:
        logger.info(
            "MCP[tavily]: skipped — SEARCH_TAVILY_API_KEY not set "
            "(get one at https://app.tavily.com/)"
        )
        return None
    config = MCPServerConfig(
        name="tavily",
        transport="stdio",
        command="npx",
        args=["-y", "tavily-mcp"],
        env={"TAVILY_API_KEY": api_key},
    )
    return MCPRegistryAdapter(
        server_config=config,
        search_tool="tavily-search",
        result_parser=parse_tavily_search,
        query_arg="query",
        extra_args={"max_results": 15, "search_depth": "advanced"},
        display_name="Tavily (MCP)",
        domains=["general", "web", "news"],
        worker_tags=["kb", "news"],
        source_type=DataSourceType.API,
        access_level=AccessLevel.API_KEY_FREE,
        requires_auth=True,
    )


# Order is informational only — registration happens in this order, but the
# router activates adapters by tag, not insertion order.
SEED_BUILDERS = [
    _build_github_adapter,
    _build_brave_search_adapter,
    _build_tavily_adapter,
]


# ──────────────────────────────────────────────────────────────────────
# Registration entry point
# ──────────────────────────────────────────────────────────────────────
def register_mcp_adapters() -> int:
    """Build and register every seed MCP adapter whose prerequisites are met.

    Returns the count of adapters actually registered. Called once at
    application startup from ``adapters.registry.register_all_adapters``.

    The function is intentionally tolerant:
      * If the ``mcp`` SDK isn't installed → log and return 0.
      * If a single builder raises → log and continue with the rest.
      * If a builder returns ``None`` (missing token) → already-logged skip.
    """
    # Cheap lazy import so the module loads even on minimal installs that
    # don't have the SDK pinned.
    try:
        from src.services.mcp_client import mcp_client
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("MCP: services.mcp_client failed to import (%s); no MCP adapters", e)
        return 0

    if not mcp_client.is_available:
        logger.info(
            "MCP: SDK not installed (`pip install mcp` / `uv add mcp`) — "
            "skipping all MCP adapter registration"
        )
        return 0

    # Optional escape hatch for tests / deployments that want MCP off.
    if os.environ.get("MCP_DISABLED", "").lower() in {"1", "true", "yes"}:
        logger.info("MCP: disabled via MCP_DISABLED env var")
        return 0

    registered = 0
    for builder in SEED_BUILDERS:
        try:
            adapter = builder()
        except Exception as e:
            logger.warning(
                "MCP: builder %s raised %s: %s",
                builder.__name__, type(e).__name__, e,
            )
            continue
        if adapter is None:
            continue
        try:
            AdapterRegistry.register(adapter)
            registered += 1
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(
                "MCP: failed to register %s: %s", builder.__name__, e
            )

    logger.info("MCP: registered %d adapters", registered)
    return registered


# Eager registration at import time, mirroring the CKAN module's pattern.
# The registry.py loader imports this module to trigger this side effect.
register_mcp_adapters()
