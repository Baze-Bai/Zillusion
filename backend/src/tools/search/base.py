"""Base search tool interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.models.candidates import RawCandidate


class BaseSearchTool(ABC):
    """Abstract base for all search engine integrations."""

    name: str = "base"
    max_results: int = 10

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if the required API key/config is available."""
        ...

    @abstractmethod
    async def search(self, query: str, max_results: int | None = None) -> list[RawCandidate]:
        """Execute search and return raw candidates."""
        ...
