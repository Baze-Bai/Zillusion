"""StreamingToolExecutor — ported from Claude Code's TypeScript implementation.

Core concurrency control pattern:
- Concurrent-safe tools (read-only) execute in parallel
- Non-concurrent tools (state-changing) execute with exclusive access
- Bash errors cascade to abort sibling tools
- Results yielded in original order while streaming progress immediately
- Context modifiers applied only by non-concurrent tools (serialized)

State machine per tool: queued → executing → completed → yielded
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class ToolStatus(str, enum.Enum):
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    YIELDED = "yielded"


class AbortReason(str, enum.Enum):
    SIBLING_ERROR = "sibling_error"
    USER_INTERRUPTED = "user_interrupted"
    TIMEOUT = "timeout"


@dataclass
class ToolProgress:
    """Intermediate progress message from a running tool."""
    tool_id: str
    message: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class ToolResult:
    """Final result from a completed tool execution."""
    tool_id: str
    success: bool
    data: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    context_modifier: Callable | None = None


@dataclass
class TrackedTool:
    """Internal state for a tracked tool in the executor."""
    id: str
    name: str
    input: dict
    is_concurrency_safe: bool
    interrupt_behavior: str = "cancel"  # cancel | block

    status: ToolStatus = ToolStatus.QUEUED
    execute_fn: Callable[..., Coroutine] | None = None
    task: asyncio.Task | None = None
    result: ToolResult | None = None
    pending_progress: list[ToolProgress] = field(default_factory=list)
    context_modifiers: list[Callable] = field(default_factory=list)
    start_time: float = 0.0
    abort_reason: AbortReason | None = None


class StreamingToolExecutor:
    """Orchestrates concurrent tool execution with safety constraints.

    Ported from Claude Code's StreamingToolExecutor pattern:
    - Read-only tools run in parallel
    - Write tools run exclusively
    - Bash errors cascade-abort siblings
    - Progress streamed immediately, results buffered in order
    """

    def __init__(self) -> None:
        self._tools: list[TrackedTool] = []
        self._progress_queue: asyncio.Queue[ToolProgress] = asyncio.Queue()
        self._sibling_abort: asyncio.Event = asyncio.Event()
        self._context_modifiers: list[Callable] = []
        self._is_aborted: bool = False

    @property
    def has_pending(self) -> bool:
        return any(t.status in (ToolStatus.QUEUED, ToolStatus.EXECUTING) for t in self._tools)

    @property
    def executing_count(self) -> int:
        return sum(1 for t in self._tools if t.status == ToolStatus.EXECUTING)

    def add_tool(
        self,
        tool_id: str,
        name: str,
        input_data: dict,
        execute_fn: Callable[..., Coroutine],
        is_concurrency_safe: bool = False,
        interrupt_behavior: str = "cancel",
    ) -> None:
        """Queue a tool for execution."""
        tool = TrackedTool(
            id=tool_id,
            name=name,
            input=input_data,
            is_concurrency_safe=is_concurrency_safe,
            interrupt_behavior=interrupt_behavior,
            execute_fn=execute_fn,
        )
        self._tools.append(tool)

    def _can_execute(self, is_concurrency_safe: bool) -> bool:
        """Check if a tool can start executing given current state.

        Rules:
        - If nothing is executing: always yes
        - If concurrent-safe: only if ALL executing tools are also concurrent-safe
        - If non-concurrent: only if nothing is executing (exclusive access)
        """
        executing = [t for t in self._tools if t.status == ToolStatus.EXECUTING]
        if not executing:
            return True
        if is_concurrency_safe:
            return all(t.is_concurrency_safe for t in executing)
        return False

    async def _execute_tool(self, tool: TrackedTool) -> None:
        """Execute a single tool with progress tracking and error handling."""
        tool.status = ToolStatus.EXECUTING
        tool.start_time = time.monotonic()

        def on_progress(message: str, data: dict | None = None) -> None:
            progress = ToolProgress(
                tool_id=tool.id,
                message=message,
                data=data or {},
            )
            tool.pending_progress.append(progress)
            self._progress_queue.put_nowait(progress)

        try:
            result = await tool.execute_fn(tool.input, on_progress)
            duration = (time.monotonic() - tool.start_time) * 1000

            tool.result = ToolResult(
                tool_id=tool.id,
                success=True,
                data=result,
                duration_ms=duration,
            )

        except asyncio.CancelledError:
            tool.abort_reason = AbortReason.SIBLING_ERROR
            tool.result = ToolResult(
                tool_id=tool.id,
                success=False,
                error=f"Aborted: {tool.abort_reason.value}",
                duration_ms=(time.monotonic() - tool.start_time) * 1000,
            )

        except Exception as e:
            duration = (time.monotonic() - tool.start_time) * 1000
            tool.result = ToolResult(
                tool_id=tool.id,
                success=False,
                error=str(e),
                duration_ms=duration,
            )

            # Bash error cascade: abort sibling tools
            if tool.name == "bash" and not tool.result.success:
                logger.warning(
                    "Bash tool %s failed, cascading abort to siblings", tool.id
                )
                self._sibling_abort.set()
                for sibling in self._tools:
                    if (
                        sibling.id != tool.id
                        and sibling.status == ToolStatus.EXECUTING
                        and sibling.task
                    ):
                        sibling.abort_reason = AbortReason.SIBLING_ERROR
                        sibling.task.cancel()

        finally:
            tool.status = ToolStatus.COMPLETED

            # Apply context modifiers (non-concurrent tools only)
            if not tool.is_concurrency_safe and tool.result and tool.result.context_modifier:
                self._context_modifiers.append(tool.result.context_modifier)

    async def process_queue(self) -> None:
        """Process the tool queue respecting concurrency rules.

        This is the main scheduling loop — call after adding tools.
        """
        tasks: list[asyncio.Task] = []

        for tool in self._tools:
            if tool.status != ToolStatus.QUEUED:
                continue

            if self._is_aborted:
                tool.result = ToolResult(
                    tool_id=tool.id, success=False, error="Executor aborted"
                )
                tool.status = ToolStatus.COMPLETED
                continue

            if self._can_execute(tool.is_concurrency_safe):
                task = asyncio.create_task(
                    self._execute_tool(tool), name=f"tool_{tool.id}"
                )
                tool.task = task
                tasks.append(task)
            else:
                # Non-concurrent tool must wait — stop processing queue
                # to maintain order (don't skip to later concurrent tools)
                if not tool.is_concurrency_safe:
                    break

        # Wait for all launched tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # After batch completes, recurse for remaining queued tools
        if any(t.status == ToolStatus.QUEUED for t in self._tools):
            await self.process_queue()

    async def execute_all(self) -> list[ToolResult]:
        """Execute all queued tools and return results in original order."""
        await self.process_queue()
        return self.get_results()

    def get_results(self) -> list[ToolResult]:
        """Get completed results in original tool order."""
        results = []
        for tool in self._tools:
            if tool.status == ToolStatus.COMPLETED and tool.result:
                tool.status = ToolStatus.YIELDED
                results.append(tool.result)
        return results

    async def get_progress(self) -> ToolProgress | None:
        """Non-blocking get of next progress message."""
        try:
            return self._progress_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def stream_progress(self):
        """Async generator yielding progress messages as they arrive."""
        while self.has_pending or not self._progress_queue.empty():
            try:
                progress = await asyncio.wait_for(
                    self._progress_queue.get(), timeout=0.1
                )
                yield progress
            except asyncio.TimeoutError:
                continue

    def abort_all(self, reason: AbortReason = AbortReason.USER_INTERRUPTED) -> None:
        """Abort all executing and queued tools."""
        self._is_aborted = True
        for tool in self._tools:
            if tool.status == ToolStatus.EXECUTING and tool.task:
                tool.abort_reason = reason
                if tool.interrupt_behavior == "cancel":
                    tool.task.cancel()
            elif tool.status == ToolStatus.QUEUED:
                tool.abort_reason = reason
                tool.result = ToolResult(
                    tool_id=tool.id, success=False, error=f"Aborted: {reason.value}"
                )
                tool.status = ToolStatus.COMPLETED

    def get_context_modifiers(self) -> list[Callable]:
        """Get accumulated context modifiers from non-concurrent tools."""
        return self._context_modifiers


# === Tool concurrency safety declarations ===
# Ported from Claude Code's per-tool isConcurrencySafe() pattern

CONCURRENT_SAFE_TOOLS = frozenset({
    # Read-only tools — safe to run in parallel
    "file_read", "read",
    "grep", "glob",
    "web_search", "web_fetch",
    "exa_search", "brave_search", "tavily_search", "searxng_search",
    "head_probe",
    "openalex_search", "semantic_scholar_search",
    "huggingface_search", "kaggle_search",
    "ckan_search", "worldbank_search",
    "github_search", "apis_guru_search",
})

NON_CONCURRENT_TOOLS = frozenset({
    # State-changing tools — must run exclusively
    "bash", "shell",
    "file_write", "file_edit",
    "firecrawl_scrape", "firecrawl_map",
})


def is_tool_concurrent_safe(tool_name: str) -> bool:
    """Check if a tool is safe for concurrent execution."""
    if tool_name in CONCURRENT_SAFE_TOOLS:
        return True
    if tool_name in NON_CONCURRENT_TOOLS:
        return False
    # Default: assume not safe (fail-closed)
    return False
