"""Multi-agent coordination — ported from Claude Code's Coordinator + Worker pattern.

Architecture:
  - Coordinator: orchestrates work, synthesizes findings, never delegates understanding
  - Workers: autonomous subagents with full tool access, domain-specialized
  - Communication: task-based with structured notifications
  - Concurrency: read-only research tasks parallel, write tasks serialized

Key principle from Claude Code:
  "Never delegate understanding" — coordinator reads worker findings,
  synthesizes into specific implementation specs with file paths,
  line numbers, and exact changes. Never: "based on your findings, fix it."
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from src.config import settings

logger = logging.getLogger(__name__)


class WorkerStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class WorkerType(str, enum.Enum):
    RESEARCH = "research"      # Read-only exploration, can run in parallel
    IMPLEMENTATION = "implementation"  # Write operations, serialized
    VALIDATION = "validation"  # Testing/verification, can parallel with research


@dataclass
class WorkerTask:
    """A task assigned to a worker agent."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    description: str = ""
    prompt: str = ""
    worker_type: WorkerType = WorkerType.RESEARCH

    # Execution state
    status: WorkerStatus = WorkerStatus.PENDING
    result: Any = None
    error: str | None = None
    start_time: float = 0.0
    end_time: float = 0.0

    # Cost tracking
    cost_usd: float = 0.0
    tokens_used: int = 0

    @property
    def duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0


@dataclass
class TaskNotification:
    """Structured notification from a completed worker task."""
    task_id: str
    task_name: str
    status: WorkerStatus
    summary: str
    result: Any = None
    duration_seconds: float = 0.0
    cost_usd: float = 0.0


@dataclass
class CoordinatorState:
    """State tracked by the coordinator across the session."""
    tasks: dict[str, WorkerTask] = field(default_factory=dict)
    completed_tasks: list[str] = field(default_factory=list)
    total_cost: float = 0.0
    total_tokens: int = 0
    iteration: int = 0


class AgentCoordinator:
    """Orchestrates multiple worker agents for complex multi-step tasks.

    Ported from Claude Code's coordinator pattern with:
    - Role separation (coordinator vs worker)
    - Concurrency control by worker type
    - Structured task notifications
    - Synthesis loop (coordinator reads → understands → directs)
    """

    def __init__(
        self,
        execute_worker: Callable[[WorkerTask], Coroutine[Any, Any, Any]],
        max_concurrent_research: int | None = None,
        max_concurrent_implementation: int | None = None,
    ) -> None:
        if max_concurrent_research is None:
            max_concurrent_research = settings.coordinator.max_concurrent_research
        if max_concurrent_implementation is None:
            max_concurrent_implementation = settings.coordinator.max_concurrent_implementation
        self._execute_worker = execute_worker
        self._max_concurrent_research = max_concurrent_research
        self._max_concurrent_implementation = max_concurrent_implementation
        self._state = CoordinatorState()
        self._notifications: asyncio.Queue[TaskNotification] = asyncio.Queue()
        self._research_semaphore = asyncio.Semaphore(max_concurrent_research)
        self._impl_semaphore = asyncio.Semaphore(max_concurrent_implementation)

    @property
    def state(self) -> CoordinatorState:
        return self._state

    def create_task(
        self,
        name: str,
        prompt: str,
        worker_type: WorkerType = WorkerType.RESEARCH,
        description: str = "",
    ) -> WorkerTask:
        """Create and register a new worker task."""
        task = WorkerTask(
            name=name,
            description=description or name,
            prompt=prompt,
            worker_type=worker_type,
        )
        self._state.tasks[task.id] = task
        return task

    async def _run_worker(self, task: WorkerTask) -> None:
        """Execute a single worker task with appropriate concurrency control."""
        # Select semaphore based on worker type
        if task.worker_type == WorkerType.IMPLEMENTATION:
            semaphore = self._impl_semaphore
        else:
            semaphore = self._research_semaphore

        async with semaphore:
            task.status = WorkerStatus.RUNNING
            task.start_time = time.monotonic()

            try:
                result = await self._execute_worker(task)
                task.status = WorkerStatus.COMPLETED
                task.result = result

            except asyncio.CancelledError:
                task.status = WorkerStatus.KILLED
                task.error = "Cancelled"

            except Exception as e:
                task.status = WorkerStatus.FAILED
                task.error = str(e)
                logger.error("Worker %s (%s) failed: %s", task.id, task.name, e)

            finally:
                task.end_time = time.monotonic()
                self._state.completed_tasks.append(task.id)

                # Emit notification
                notification = TaskNotification(
                    task_id=task.id,
                    task_name=task.name,
                    status=task.status,
                    summary=str(task.result)[:500] if task.result else task.error or "",
                    result=task.result,
                    duration_seconds=task.duration_seconds,
                    cost_usd=task.cost_usd,
                )
                await self._notifications.put(notification)

    async def spawn_workers(self, tasks: list[WorkerTask]) -> list[TaskNotification]:
        """Spawn multiple workers respecting concurrency rules.

        Research tasks run in parallel (up to max_concurrent_research).
        Implementation tasks run serially (max_concurrent_implementation=1).
        Validation tasks can run alongside research.
        """
        if not tasks:
            return []

        # Launch all tasks — semaphores handle concurrency
        worker_tasks = [
            asyncio.create_task(self._run_worker(task), name=f"worker_{task.id}")
            for task in tasks
        ]

        # Wait for all to complete
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        # Collect notifications
        notifications = []
        while not self._notifications.empty():
            notifications.append(await self._notifications.get())

        # Update state
        self._state.iteration += 1
        for task in tasks:
            self._state.total_cost += task.cost_usd
            self._state.total_tokens += task.tokens_used

        return notifications

    async def spawn_parallel_research(
        self, tasks: list[WorkerTask]
    ) -> list[TaskNotification]:
        """Convenience: spawn research-only tasks in parallel."""
        for task in tasks:
            task.worker_type = WorkerType.RESEARCH
        return await self.spawn_workers(tasks)

    async def spawn_serial_implementation(
        self, tasks: list[WorkerTask]
    ) -> list[TaskNotification]:
        """Convenience: spawn implementation tasks serially."""
        notifications = []
        for task in tasks:
            task.worker_type = WorkerType.IMPLEMENTATION
            result = await self.spawn_workers([task])
            notifications.extend(result)
        return notifications

    def get_task(self, task_id: str) -> WorkerTask | None:
        return self._state.tasks.get(task_id)

    def kill_task(self, task_id: str) -> bool:
        """Kill a running task."""
        task = self._state.tasks.get(task_id)
        if task and task.status == WorkerStatus.RUNNING:
            task.status = WorkerStatus.KILLED
            return True
        return False

    def get_active_tasks(self) -> list[WorkerTask]:
        return [t for t in self._state.tasks.values() if t.status == WorkerStatus.RUNNING]

    def get_completed_notifications(self) -> list[TaskNotification]:
        """Non-blocking: get all available notifications."""
        notifications = []
        while not self._notifications.empty():
            try:
                notifications.append(self._notifications.get_nowait())
            except asyncio.QueueEmpty:
                break
        return notifications
