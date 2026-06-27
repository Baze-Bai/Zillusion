"""Adapter registry — singleton that all adapters register into at import time."""

from __future__ import annotations

import logging
from typing import Any

from src.adapters.base import BaseRegistryAdapter, HealthStatus

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """Singleton registry for all data source adapters."""

    _adapters: dict[str, BaseRegistryAdapter] = {}

    @classmethod
    def register(cls, adapter: BaseRegistryAdapter) -> None:
        """Register an adapter instance."""
        cls._adapters[adapter.name] = adapter
        logger.info("Registered adapter: %s (%s)", adapter.name, adapter.display_name)

    @classmethod
    def get(cls, name: str) -> BaseRegistryAdapter | None:
        """Get a specific adapter by name."""
        return cls._adapters.get(name)

    @classmethod
    def get_by_worker(cls, worker_tag: str) -> list[BaseRegistryAdapter]:
        """Get all adapters for a given worker tag."""
        return [
            a for a in cls._adapters.values()
            if worker_tag in a.worker_tags
        ]

    @classmethod
    def get_all(cls) -> list[BaseRegistryAdapter]:
        """Get all registered adapters."""
        return list(cls._adapters.values())

    @classmethod
    async def health_check_all(cls) -> dict[str, HealthStatus]:
        """Run health checks on all adapters."""
        import asyncio
        results = {}
        checks = {
            name: adapter.health_check()
            for name, adapter in cls._adapters.items()
        }
        for name, coro in checks.items():
            try:
                results[name] = await coro
            except Exception as e:
                results[name] = HealthStatus(is_healthy=False, error=str(e))
        return results


def register_all_adapters() -> None:
    """Import all adapter modules to trigger registration."""
    try:
        from src.adapters.academic import openalex  # noqa: F401
    except ImportError:
        pass
    try:
        from src.adapters.academic import semantic_scholar  # noqa: F401
    except ImportError:
        pass
    try:
        from src.adapters.datasets import huggingface  # noqa: F401
    except ImportError:
        pass
    try:
        from src.adapters.datasets import kaggle  # noqa: F401
    except ImportError:
        pass
    try:
        from src.adapters.government import ckan  # noqa: F401
    except ImportError:
        pass
    # MCP-backed adapters (GitHub / Brave / Tavily / …) — module performs
    # token-checked registration at import time; missing tokens or a missing
    # `mcp` SDK degrade silently (logged, not raised) so this import is safe
    # to leave unguarded structurally, but we still wrap it for parity with
    # the other adapter imports above.
    try:
        from src.adapters.mcp import servers as _mcp_servers  # noqa: F401
    except ImportError as e:
        logger.info("MCP adapter module unavailable: %s", e)
    logger.info("Registered %d adapters total", len(AdapterRegistry._adapters))

    # Surface the post-registration state to the run log so any RUN can
    # see what registry surface area was available at the time
    # (cross-checking with query_registry call sites). Done as a local
    # import to avoid coupling adapter import order to run_logger import
    # at module-evaluation time.
    try:
        from src.services.run_logger import log_event
        all_adapters = AdapterRegistry.get_all()
        by_worker: dict[str, list[str]] = {}
        details: list[dict[str, Any]] = []
        for a in all_adapters:
            for tag in a.worker_tags:
                by_worker.setdefault(tag, []).append(a.name)
            details.append({
                "name": a.name,
                "display_name": getattr(a, "display_name", a.name),
                "worker_tags": list(a.worker_tags or []),
                "domains": list(getattr(a, "domains", []) or []),
                "base_url": getattr(a, "base_url", "") or "",
                "requires_auth": bool(getattr(a, "requires_auth", False)),
                "module": type(a).__module__,
                "class": type(a).__name__,
            })
        log_event("adapters_ready", {
            "n_adapters": len(all_adapters),
            "adapter_names": sorted(a.name for a in all_adapters),
            "by_worker_tag": {k: sorted(v) for k, v in by_worker.items()},
            "adapters": details,
        })
    except Exception:
        # Never let logging cause registration to fail.
        pass
