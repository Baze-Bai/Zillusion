"""Pydantic schema for workspaces/<site>/download_manifest.yaml.

The DOWNLOAD analog of selectors.yaml. When an explore agent determines a
source is a *file* data source (a downloadable dataset, not embedded page
data), it produces a DOWNLOAD workflow + this manifest, declaring exactly
which file(s) workflow.py downloads and how to verify them. The validator
re-downloads in isolation and checks each file against this manifest.

Mutually exclusive with selectors.yaml: a run produces one or the other,
matching its workflow_type. The manifest's *presence* is how the validator
detects download mode (vs selectors.yaml → extraction mode).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class DownloadTarget(BaseModel):
    """One file the workflow downloads."""

    url: str = Field(..., min_length=1, description="Absolute URL workflow.py downloads from.")
    filename: str = Field(..., min_length=1, description="Local filename written under output_dir/.")
    file_format: str = Field(
        ..., description="Lowercase format token: csv / json / xlsx / parquet / xml / zip / ..."
    )
    min_bytes: int = Field(
        default=1, ge=0, description="Expected minimum size in bytes; 0 disables the non-empty check."
    )
    columns: list[str] | None = Field(
        default=None,
        description="Expected column/field names if known — a HYPOTHESIS for sanity, not a hard gate.",
    )
    observation: str = Field(default="", description="One-line note: where the link was found, any gotcha.")

    model_config = {"extra": "forbid"}


class DownloadManifestFile(BaseModel):
    """Top-level shape of workspaces/<site>/download_manifest.yaml."""

    workflow_type: Literal["download"] = "download"
    source_url: str = Field(..., description="The page the download target(s) were found on.")
    output_dir: str = Field(
        default="downloads", description="Directory (relative to workflow.py) the files land in."
    )
    files: list[DownloadTarget] = Field(..., min_length=1, description="One entry per downloaded file.")
    notes: str = Field(default="", description="Free notes (e.g. landing-page → file-URL resolution).")

    model_config = {"extra": "forbid"}

    @classmethod
    def load(cls, path: Path) -> "DownloadManifestFile":
        if not path.exists():
            raise FileNotFoundError(
                f"download_manifest.yaml not found at {path}. A download workflow must write it "
                f"via the download_manifest_write MCP tool."
            )
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"download_manifest.yaml at {path} is unparseable YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"download_manifest.yaml at {path} has wrong top-level shape: "
                f"got {type(data).__name__}, expected mapping."
            )
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            self.model_dump(), allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
        )
        header = (
            "# workspaces/<site>/download_manifest.yaml — managed by mcp_server.schemas.DownloadManifestFile\n"
            "# Declares the file(s) a DOWNLOAD workflow.py fetches; the validator re-downloads + verifies each.\n"
            "# Mutually exclusive with selectors.yaml (extraction). Its presence = download workflow_type.\n"
            "\n"
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(header + text, encoding="utf-8")
        tmp.replace(path)


__all__ = ["DownloadTarget", "DownloadManifestFile"]
