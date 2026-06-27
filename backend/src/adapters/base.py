"""Base registry adapter — the contract every data source registry must implement."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.models.candidates import RawCandidate
from src.models.data_source import DataSource

logger = logging.getLogger("adapter.health")


@dataclass
class RateLimitConfig:
    requests_per_second: float = 5.0
    burst: int = 10


@dataclass
class HealthStatus:
    is_healthy: bool
    latency_ms: float = 0.0
    error: str | None = None


class BaseRegistryAdapter(ABC):
    """Abstract base for all registry adapters (OpenAlex, HuggingFace, CKAN, etc.)."""

    name: str = "base"
    display_name: str = "Base Adapter"
    domains: list[str] = []  # Which data domains this serves
    worker_tags: list[str] = []  # e.g., ["academic"], ["datasets"]
    rate_limit: RateLimitConfig = RateLimitConfig()
    requires_auth: bool = False
    base_url: str = ""

    @abstractmethod
    async def search(
        self, keywords: list[str], filters: dict
    ) -> list[RawCandidate]:
        """Search the registry and return raw candidates."""
        ...

    @abstractmethod
    def normalize(self, raw: dict) -> DataSource:
        """Convert a raw API response item to the unified DataSource schema."""
        ...

    async def health_check(self) -> HealthStatus:
        """Check if the registry API is reachable.

        Per-adapter results are emitted at DEBUG — at INFO they used to
        produce 23+ lines on every container start, drowning the actually
        interesting startup events. The summary line in
        `src.services.prefetch._prefetch_adapter_health` carries the same
        signal in one line (healthy count + names of any that failed).
        """
        import time
        import httpx

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.base_url)
                latency_ms = round((time.monotonic() - start) * 1000, 2)
                healthy = resp.status_code < 500
                logger.debug(
                    "adapter.health_check",
                    extra={
                        "event": "adapter.health_check",
                        "adapter": self.name,
                        "base_url": self.base_url,
                        "status": resp.status_code,
                        "healthy": healthy,
                        "latency_ms": latency_ms,
                    },
                )
                return HealthStatus(is_healthy=healthy, latency_ms=latency_ms)
        except Exception as e:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            logger.debug(
                "adapter.health_check_failed",
                extra={
                    "event": "adapter.health_check_failed",
                    "adapter": self.name,
                    "base_url": self.base_url,
                    "error_type": type(e).__name__,
                    "error": str(e),
                    "latency_ms": latency_ms,
                },
            )
            return HealthStatus(is_healthy=False, error=str(e), latency_ms=latency_ms)

    def get_metadata_signals(self, raw: dict) -> dict:
        """Extract authority-relevant metadata (citations/downloads/stars).

        Override in subclasses to provide adapter-specific signals.
        """
        return {}
