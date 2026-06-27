"""MCP (Model Context Protocol) client.

Wraps external MCP servers (stdio subprocess, SSE, streamable HTTP) so the
adapter layer can fan out to GitHub / Brave / arXiv / Wikipedia / fetch / etc.
through a single uniform path.

Tool naming convention: mcp__{server_name}__{tool_name}

Lifecycle
---------
The Python ``mcp`` SDK exposes connections as async context managers. We
keep them open across many ``call_tool`` invocations by parking them on an
``AsyncExitStack`` owned by the singleton; ``disconnect_all`` (called from
the FastAPI shutdown hook in ``main.py``) tears them all down at once.

Defensive imports
-----------------
The ``mcp`` package is declared in ``pyproject.toml`` but the module still
imports cleanly when it's missing — handy for slim CI images and for users
who haven't run ``uv sync`` yet. ``MCPClient.is_available`` reports the
truth so the adapter layer can skip registration silently.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Defensive SDK import — module must load even if `mcp` is absent.
# ──────────────────────────────────────────────────────────────────────
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised on minimal installs
    MCP_SDK_AVAILABLE = False
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment]

try:
    from mcp.client.sse import sse_client  # type: ignore
    HAS_SSE_TRANSPORT = True
except ImportError:
    HAS_SSE_TRANSPORT = False
    sse_client = None  # type: ignore[assignment]

try:
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore
    HAS_HTTP_TRANSPORT = True
except ImportError:
    HAS_HTTP_TRANSPORT = False
    streamablehttp_client = None  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────
class MCPDisabledError(RuntimeError):
    """Raised when call_tool targets a server that's permanently disabled.

    Distinct from a transient failure so callers (notably
    `MCPRegistryAdapter.search`) can skip the warning log on the per-request
    fan-out — the prefetch already logged the disabled state once.
    """


# ──────────────────────────────────────────────────────────────────────
# Public dataclasses
# ──────────────────────────────────────────────────────────────────────
@dataclass
class MCPToolDefinition:
    """A tool discovered from an MCP server."""
    name: str               # Full name: mcp__{server}__{tool}
    server_name: str
    tool_name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    is_concurrent_safe: bool = True


@dataclass
class MCPResource:
    """A resource available from an MCP server."""
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


@dataclass
class MCPServerConfig:
    """Declarative config for an MCP server — registered up-front, dialled lazily."""
    name: str
    transport: str  # "stdio" | "sse" | "http"
    # stdio
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # sse / http
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class MCPServerConnection:
    """Live (or failed) connection to one MCP server."""
    name: str
    transport_type: str = "stdio"
    url: str = ""
    is_connected: bool = False
    tools: list[MCPToolDefinition] = field(default_factory=list)
    resources: list[MCPResource] = field(default_factory=list)
    error: str | None = None
    session: Any = None  # ClientSession when connected
    # When True, this server has failed in a way that won't recover within the
    # process lifetime (e.g. stdio binary not on $PATH). `call_tool` will
    # short-circuit silently instead of retrying + emitting WARNING per call.
    permanently_failed: bool = False


# ──────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────
class MCPClient:
    """Singleton client managing persistent MCP connections.

    Two-phase usage:
      1. ``register_config(MCPServerConfig)`` at import/startup time — cheap,
         no I/O, no subprocess spawn.
      2. First ``call_tool`` / ``list_tools`` triggers ``_connect`` which
         attaches the underlying ``ClientSession`` to the shared
         ``AsyncExitStack``.
    """

    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._connections: dict[str, MCPServerConnection] = {}
        self._tool_executors: dict[str, Callable] = {}
        self._stack: AsyncExitStack | None = None

    @property
    def is_available(self) -> bool:
        """True iff the MCP SDK imported successfully."""
        return MCP_SDK_AVAILABLE

    # ── config registry ────────────────────────────────────────────
    def register_config(self, config: MCPServerConfig) -> None:
        """Record a server config for lazy connection."""
        self._configs[config.name] = config

    def get_registered_configs(self) -> list[MCPServerConfig]:
        return list(self._configs.values())

    # ── connection lifecycle ──────────────────────────────────────
    async def _ensure_stack(self) -> AsyncExitStack:
        if self._stack is None:
            self._stack = AsyncExitStack()
            await self._stack.__aenter__()
        return self._stack

    async def _connect(self, config: MCPServerConfig) -> MCPServerConnection:
        """Open transport + initialize session + discover tools."""
        conn = MCPServerConnection(
            name=config.name,
            transport_type=config.transport,
            url=config.url,
        )

        if not MCP_SDK_AVAILABLE:
            conn.error = "mcp SDK not installed (pip install mcp)"
            self._connections[config.name] = conn
            return conn

        try:
            stack = await self._ensure_stack()

            if config.transport == "stdio":
                if stdio_client is None:
                    raise RuntimeError("stdio transport unavailable in this SDK")
                params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=config.env or None,
                )
                read, write = await stack.enter_async_context(stdio_client(params))

            elif config.transport == "sse":
                if not HAS_SSE_TRANSPORT or sse_client is None:
                    raise RuntimeError("SSE transport unavailable in this SDK version")
                read, write = await stack.enter_async_context(
                    sse_client(config.url, headers=config.headers or None)
                )

            elif config.transport == "http":
                if not HAS_HTTP_TRANSPORT or streamablehttp_client is None:
                    raise RuntimeError(
                        "Streamable HTTP transport unavailable in this SDK version"
                    )
                # streamablehttp_client yields (read, write, get_session_id)
                triple = await stack.enter_async_context(
                    streamablehttp_client(config.url, headers=config.headers or None)
                )
                read, write = triple[0], triple[1]

            else:
                raise ValueError(f"Unknown transport: {config.transport!r}")

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            conn.session = session
            conn.is_connected = True

            # Discover tools
            try:
                tools_resp = await session.list_tools()
                for t in tools_resp.tools:
                    conn.tools.append(MCPToolDefinition(
                        name=f"mcp__{config.name}__{t.name}",
                        server_name=config.name,
                        tool_name=t.name,
                        description=getattr(t, "description", "") or "",
                        input_schema=getattr(t, "inputSchema", {}) or {},
                    ))
            except Exception as e:
                logger.debug("MCP %s: list_tools failed (server may not expose any): %s",
                             config.name, e)

            # Discover resources (optional — many servers don't expose any)
            try:
                res_resp = await session.list_resources()
                for r in getattr(res_resp, "resources", []):
                    conn.resources.append(MCPResource(
                        uri=str(getattr(r, "uri", "")),
                        name=getattr(r, "name", ""),
                        description=getattr(r, "description", "") or "",
                        mime_type=getattr(r, "mimeType", "") or "",
                    ))
            except Exception:
                pass  # most servers don't implement resources/list

            logger.info(
                "MCP connected: %s (%s) — %d tools, %d resources",
                config.name, config.transport, len(conn.tools), len(conn.resources),
            )

        except Exception as e:
            conn.error = str(e)
            conn.is_connected = False
            # If the failure can't recover within this process — stdio binary
            # not on PATH, missing SDK, etc. — mark it so future call_tool
            # invocations short-circuit silently. Otherwise the warning
            # repeats on every /discover request for the rest of the process.
            if isinstance(e, FileNotFoundError) or "No such file or directory" in str(e):
                conn.permanently_failed = True
            logger.warning(
                "MCP connection failed: %s (%s) — %s: %s%s",
                config.name, config.transport, type(e).__name__, e,
                " [permanently disabled]" if conn.permanently_failed else "",
            )

        self._connections[config.name] = conn
        return conn

    async def get_or_connect(self, server_name: str) -> MCPServerConnection:
        """Return existing connection or open one from registered config."""
        existing = self._connections.get(server_name)
        if existing is not None:
            return existing
        config = self._configs.get(server_name)
        if config is None:
            raise RuntimeError(f"MCP server '{server_name}' has no registered config")
        return await self._connect(config)

    # Back-compat alias for anything still calling the old API.
    async def connect(
        self,
        server_name: str,
        transport_type: str = "stdio",
        url: str = "",
        command: str = "",
        args: list[str] | None = None,
    ) -> MCPServerConnection:
        config = MCPServerConfig(
            name=server_name,
            transport=transport_type,
            url=url,
            command=command,
            args=args or [],
        )
        self.register_config(config)
        return await self._connect(config)

    # ── tool invocation ───────────────────────────────────────────
    def register_tool_executor(
        self, full_tool_name: str, executor: Callable[..., Coroutine]
    ) -> None:
        """Register a Python callable as a fake MCP tool — bypasses the protocol."""
        self._tool_executors[full_tool_name] = executor

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> Any:
        """Invoke a tool on an MCP server (or a registered local executor)."""
        import time

        full_name = f"mcp__{server_name}__{tool_name}"
        start = time.monotonic()
        arg_keys = list(arguments.keys()) if isinstance(arguments, dict) else []

        # Fast-path the permanent-failure case BEFORE emitting mcp.call.start,
        # so a disabled server contributes zero log lines per request.
        existing = self._connections.get(server_name)
        if existing is not None and existing.permanently_failed:
            logger.debug(
                "mcp.call.skipped_permanently_failed",
                extra={
                    "event": "mcp.call.skipped_permanently_failed",
                    "server": server_name,
                    "tool": tool_name,
                    "error": existing.error,
                },
            )
            raise MCPDisabledError(
                f"MCP server '{server_name}' permanently disabled: {existing.error}"
            )

        logger.info(
            "mcp.call.start",
            extra={
                "event": "mcp.call.start",
                "server": server_name,
                "tool": tool_name,
                "full_name": full_name,
                "arg_keys": arg_keys,
            },
        )

        try:
            # Local executors win over real MCP servers — useful for tests.
            if full_name in self._tool_executors:
                result = await self._tool_executors[full_name](arguments)
                logger.info(
                    "mcp.call.ok",
                    extra={
                        "event": "mcp.call.ok",
                        "server": server_name,
                        "tool": tool_name,
                        "duration_ms": round((time.monotonic() - start) * 1000, 2),
                        "executor": "registered",
                    },
                )
                return result

            conn = await self.get_or_connect(server_name)
            if not conn.is_connected or conn.session is None:
                logger.warning(
                    "mcp.call.not_connected",
                    extra={
                        "event": "mcp.call.not_connected",
                        "server": server_name,
                        "tool": tool_name,
                        "error": conn.error,
                    },
                )
                raise RuntimeError(
                    f"MCP server '{server_name}' not connected: {conn.error}"
                )

            result = await conn.session.call_tool(tool_name, arguments)
            logger.info(
                "mcp.call.ok",
                extra={
                    "event": "mcp.call.ok",
                    "server": server_name,
                    "tool": tool_name,
                    "duration_ms": round((time.monotonic() - start) * 1000, 2),
                    "executor": "session",
                },
            )
            return result
        except Exception as e:
            logger.warning(
                "mcp.call.failed",
                extra={
                    "event": "mcp.call.failed",
                    "server": server_name,
                    "tool": tool_name,
                    "error_type": type(e).__name__,
                    "error": str(e),
                    "duration_ms": round((time.monotonic() - start) * 1000, 2),
                },
            )
            raise

    # ── introspection ─────────────────────────────────────────────
    async def list_tools(self, server_name: str) -> list[MCPToolDefinition]:
        conn = await self.get_or_connect(server_name)
        return conn.tools

    async def list_resources(self, server_name: str) -> list[MCPResource]:
        conn = self._connections.get(server_name)
        return conn.resources if conn else []

    async def read_resource(self, server_name: str, uri: str) -> str:
        conn = await self.get_or_connect(server_name)
        if not conn.is_connected or conn.session is None:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        result = await conn.session.read_resource(uri)
        return str(result)

    def get_all_tools(self) -> list[MCPToolDefinition]:
        tools: list[MCPToolDefinition] = []
        for conn in self._connections.values():
            if conn.is_connected:
                tools.extend(conn.tools)
        return tools

    def get_connection(self, name: str) -> MCPServerConnection | None:
        return self._connections.get(name)

    def get_all_connections(self) -> list[MCPServerConnection]:
        return list(self._connections.values())

    # ── shutdown ──────────────────────────────────────────────────
    async def disconnect(self, server_name: str) -> None:
        """Mark a single connection as disconnected. Real teardown happens in disconnect_all."""
        conn = self._connections.get(server_name)
        if conn:
            conn.is_connected = False
            conn.session = None
            logger.info("MCP disconnected: %s", server_name)

    async def disconnect_all(self) -> None:
        """Tear down every open MCP transport. Called from FastAPI shutdown."""
        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except Exception as e:  # pragma: no cover — best-effort cleanup
                logger.warning("Error during MCP stack cleanup: %s", e)
            self._stack = None
        for conn in self._connections.values():
            conn.is_connected = False
            conn.session = None
        logger.info("MCP disconnect_all: closed %d connections", len(self._connections))


# ──────────────────────────────────────────────────────────────────────
# Internal-tool wrapper (kept for back-compat — used by older code paths)
# ──────────────────────────────────────────────────────────────────────
def wrap_as_mcp_tools(tools: dict[str, Callable]) -> list[MCPToolDefinition]:
    """Expose internal Python callables as MCP-shaped tool definitions."""
    return [
        MCPToolDefinition(
            name=f"mcp__internal__{name}",
            server_name="internal",
            tool_name=name,
            description=getattr(executor, "__doc__", "") or "",
        )
        for name, executor in tools.items()
    ]


# Singleton — imported by main.py shutdown hook and by MCPRegistryAdapter.
mcp_client = MCPClient()
