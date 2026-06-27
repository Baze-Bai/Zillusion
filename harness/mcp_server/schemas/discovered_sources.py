"""Pydantic schema for workspaces/<site>/discovered_sources.yaml.

When an explore agent finds, MID-RUN, a genuinely NEW data product that
discovery never identified and no sibling run is handling (a different URL, or
a category nobody here owns), it reports it here via the `report_discovered_source`
MCP tool — instead of handling it inline (wrong workflow type) or dropping it.

The orchestrator (scripts/discovery_to_harness.py) harvests this file after the
run and stages a NEW site per reported product (deduped by (url, type), capped).
Append-friendly: a list, one entry per reported product.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class DiscoveredSource(BaseModel):
    """One newly-found data product an explore agent reports for its own run."""

    url: str = Field(..., min_length=1, description="URL of the newly-found data product.")
    types: list[Literal["embedded", "file", "api"]] = Field(
        ..., min_length=1, description="Its data category/categories (one or more)."
    )
    note: str = Field(default="", description="What it is / why it's a separate product worth its own run.")
    found_on_url: str = Field(default="", description="The page you were on when you found it.")

    model_config = {"extra": "forbid"}


class DiscoveredSourcesFile(BaseModel):
    """Top-level shape of workspaces/<site>/discovered_sources.yaml."""

    sources: list[DiscoveredSource] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @classmethod
    def load(cls, path: Path) -> "DiscoveredSourcesFile":
        """Load (or empty if absent — most runs report nothing)."""
        if not path.exists():
            return cls(sources=[])
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            return cls(sources=[])
        if isinstance(data, list):  # tolerate a bare list
            return cls(sources=data)
        if not isinstance(data, dict):
            raise ValueError(
                f"discovered_sources.yaml at {path} has wrong top-level shape: {type(data).__name__}"
            )
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            self.model_dump(), allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
        )
        header = (
            "# workspaces/<site>/discovered_sources.yaml — managed by mcp_server.schemas.DiscoveredSourcesFile\n"
            "# NEW data products found mid-run that need their OWN run; harvested by the orchestrator\n"
            "# (scripts/discovery_to_harness.py). Written via the report_discovered_source MCP tool.\n"
            "\n"
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(header + text, encoding="utf-8")
        tmp.replace(path)


__all__ = ["DiscoveredSource", "DiscoveredSourcesFile"]
