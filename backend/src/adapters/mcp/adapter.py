"""Generic MCP-backed registry adapter.

One class — many servers. Each instance binds a registered
:class:`MCPServerConfig` to a "search-like" MCP tool, plus a small parser
that turns the tool's ``CallToolResult`` into a list of dicts the rest of
the pipeline understands.

Why a callable parser instead of a JSON-path config?
  Different MCP servers return wildly different shapes:
    - Brave returns ``{"web":{"results":[…]}}`` JSON in a TextContent block.
    - GitHub returns a repo array.
    - fetch returns a single page of HTML/text.
  A small Python function per server is more honest than pretending one
  declarative schema can express all of those.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

from src.adapters.base import BaseRegistryAdapter, RateLimitConfig
from src.models.candidates import RawCandidate
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.services.mcp_client import MCPDisabledError, MCPServerConfig, mcp_client
from src.services.run_logger import log_event

logger = logging.getLogger(__name__)


# Parser signature: (raw_call_tool_result) -> list of {url,title,description,...}
ResultParser = Callable[[Any], list[dict]]


class MCPRegistryAdapter(BaseRegistryAdapter):
    """Wraps one MCP server as a :class:`BaseRegistryAdapter`.

    Args:
        server_config: The :class:`MCPServerConfig` to register with the
            global ``mcp_client``. Connection is opened lazily on first
            ``search`` call.
        search_tool: Name of the MCP tool to invoke for searches (e.g.
            ``"brave_web_search"`` or ``"search_repositories"``).
        result_parser: Server-specific function that flattens the
            :class:`CallToolResult` into a list of dicts.
        query_arg: The argument name the tool uses for the query string
            (defaults to ``"query"`` — overridden per-server when needed).
        extra_args: Optional fixed kwargs passed on every call (e.g.
            ``{"count": 15}``).
        domains / worker_tags / source_type / access_level: Forwarded onto
            the resulting :class:`RawCandidate` and :class:`DataSource`.
    """

    # Class-level defaults; every instance overrides ``name`` etc. in __init__.
    name = "mcp"
    display_name = "MCP"

    def __init__(
        self,
        *,
        server_config: MCPServerConfig,
        search_tool: str,
        result_parser: ResultParser,
        query_arg: str = "query",
        extra_args: dict | None = None,
        display_name: str | None = None,
        domains: list[str] | None = None,
        worker_tags: list[str] | None = None,
        source_type: DataSourceType = DataSourceType.API,
        access_level: AccessLevel = AccessLevel.UNKNOWN,
        rate_limit: RateLimitConfig | None = None,
        requires_auth: bool = False,
    ) -> None:
        self._server_config = server_config
        self._search_tool = search_tool
        # Resolved lazily on first search: the configured search_tool can drift
        # out of sync with the installed MCP server version (e.g. tavily-mcp
        # renamed its tool, yielding `-32601 Unknown tool`). None = unresolved.
        self._resolved_tool: str | None = None
        self._result_parser = result_parser
        self._query_arg = query_arg
        self._extra_args = dict(extra_args or {})

        self.name = f"mcp_{server_config.name}"
        self.display_name = display_name or f"MCP: {server_config.name}"
        self.domains = list(domains or [])
        self.worker_tags = list(worker_tags or ["mcp"])
        self.rate_limit = rate_limit or RateLimitConfig(requests_per_second=2.0, burst=5)
        self.requires_auth = requires_auth
        self.base_url = server_config.url or f"mcp://{server_config.name}"

        self._source_type = source_type
        self._access_level = access_level

        # Register with the singleton — connection is still lazy.
        mcp_client.register_config(server_config)

    # ── Tool-name resolution ──────────────────────────────────────
    async def _resolve_tool_name(self) -> str:
        """Resolve the actual MCP tool name, tolerating server-side renames.

        Lists the server's tools once (cached), and falls back to a tool whose
        name contains "search" when the configured ``search_tool`` is absent.
        Any failure → use the configured name unchanged (never blocks search).
        This is what makes the adapter survive MCP-server version drift (the
        root cause of the historical ``-32601 Unknown tool: tavily-search``).
        """
        if self._resolved_tool is not None:
            return self._resolved_tool
        configured = self._search_tool
        try:
            tools = await mcp_client.list_tools(self._server_config.name)
            names = [n for n in (getattr(t, "name", None) for t in (tools or [])) if n]
            if not names or configured in names:
                self._resolved_tool = configured
            else:
                match = next((n for n in names if "search" in n.lower()), None)
                resolved = match or names[0]
                logger.warning(
                    "MCP %s: configured tool %r not found; using %r (available=%s)",
                    self.name, configured, resolved, names,
                )
                log_event("mcp_tool_resolved", {
                    "mcp_server": self._server_config.name,
                    "configured": configured, "resolved": resolved,
                    "available": names,
                })
                self._resolved_tool = resolved
        except Exception as e:
            logger.debug(
                "MCP %s: tool resolution failed (%s); using configured %r",
                self.name, e, configured,
            )
            self._resolved_tool = configured
        return self._resolved_tool

    # ── BaseRegistryAdapter contract ──────────────────────────────
    async def search(
        self, keywords: list[str], filters: dict
    ) -> list[RawCandidate]:
        if not mcp_client.is_available:
            logger.debug("MCP SDK unavailable; %s.search short-circuits to []", self.name)
            return []

        query = " ".join(keywords[:5]).strip()
        if not query:
            return []

        arguments = {self._query_arg: query, **self._extra_args}

        tool_name = await self._resolve_tool_name()

        try:
            result = await mcp_client.call_tool(
                self._server_config.name, tool_name, arguments
            )
        except MCPDisabledError:
            # Server is permanently disabled (e.g. stdio binary missing). The
            # connection-time WARNING already explained why; emitting another
            # WARNING per /discover request is just noise.
            return []
        except Exception as e:
            logger.warning("MCP %s search failed: %s", self.name, e)
            # Structured so MCP failures land in the run-log + diagnostics
            # tool_errors (keyed by the user-facing query_registry tool).
            log_event("tool_error", {
                "tool": "query_registry", "adapter": self.name,
                "mcp_server": self._server_config.name, "mcp_tool": tool_name,
                "error": f"{type(e).__name__}: {e}",
            })
            return []

        try:
            items = self._result_parser(result) or []
        except Exception as e:
            logger.warning("MCP %s parser raised %s: %s", self.name, type(e).__name__, e)
            log_event("tool_error", {
                "tool": "query_registry", "adapter": self.name,
                "error": f"parser: {type(e).__name__}: {e}",
            })
            return []

        candidates: list[RawCandidate] = []
        for item in items:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            candidates.append(
                RawCandidate(
                    url=url,
                    title=(item.get("title") or "")[:300],
                    snippet=(item.get("description") or item.get("snippet") or "")[:300],
                    source_engine=f"mcp_{self._server_config.name}",
                    discovery_method="registry",
                    known_type=self._source_type,
                    metadata={
                        **item,
                        "mcp_server": self._server_config.name,
                        "mcp_tool": self._search_tool,
                    },
                )
            )

        logger.debug(
            "MCP %s returned %d candidates for: %s",
            self.name, len(candidates), query[:50],
        )
        return candidates

    def normalize(self, raw: dict) -> DataSource:
        url = raw.get("url", "")
        source_id = hashlib.sha256(
            f"{self._server_config.name}/{url}".encode()
        ).hexdigest()[:16]

        return DataSource(
            id=source_id,
            name=raw.get("title", url) or url,
            provider=self.display_name,
            url=url,
            source_type=self._source_type,
            description=(raw.get("description") or raw.get("snippet") or "")[:500],
            domain=(self.domains[0] if self.domains else "general"),
            tags=raw.get("tags", []) if isinstance(raw.get("tags"), list) else [],
            data_format=raw.get("data_format", []) if isinstance(raw.get("data_format"), list) else [],
            access_level=self._access_level,
            license=raw.get("license"),
            discovery_method="registry",
            metadata={**raw, "mcp_server": self._server_config.name},
        )

    # ── Health check ─────────────────────────────────────────────
    async def health_check(self):  # type: ignore[override]
        """An MCP server is healthy iff a session can be opened and tools listed."""
        from src.adapters.base import HealthStatus
        import time
        start = time.monotonic()

        if not mcp_client.is_available:
            return HealthStatus(
                is_healthy=False,
                error="mcp SDK not installed",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
            )
        try:
            conn = await mcp_client.get_or_connect(self._server_config.name)
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            if conn.is_connected:
                return HealthStatus(is_healthy=True, latency_ms=latency_ms)
            return HealthStatus(
                is_healthy=False, error=conn.error or "not connected",
                latency_ms=latency_ms,
            )
        except Exception as e:
            return HealthStatus(
                is_healthy=False, error=str(e),
                latency_ms=round((time.monotonic() - start) * 1000, 2),
            )
