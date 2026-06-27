"""Pydantic schema for workspaces/<site>/api_manifest.yaml.

The API analog of selectors.yaml / download_manifest.yaml. When an explore
agent determines a source is an *API* data source (records fetched over
HTTP, not embedded page data and not a downloadable file), it produces an
API workflow + this manifest, declaring exactly which endpoint(s)
workflow.py calls, how requests authenticate, and which output fields the
records carry. The validator re-calls the declared ``probe_url`` in
isolation and checks the live response against the workflow's output.

Mutually exclusive with selectors.yaml and download_manifest.yaml: a run
produces exactly one of the three, matching its workflow_type. The
manifest's *presence* is how the validator detects api mode.

``credentials_source`` describes WHERE workflow.py reads its secret from
(an env var name, a credentials.json walk-up) — it must NEVER contain a
secret value itself; the validator's secret-leak check scans this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from mcp_server.schemas.selectors import FieldStabilityClass


class APIEndpointAuth(BaseModel):
    """How requests to one endpoint authenticate. ``None`` on the endpoint = no auth."""

    type: Literal["api_key", "bearer", "basic", "none"] = Field(
        ..., description="Auth scheme workflow.py applies to this endpoint."
    )
    location: Literal["header", "query"] = Field(
        default="header",
        description="Where the credential goes: a request header or a query param.",
    )
    param_name: str | None = Field(
        default=None,
        description="Header/query-param name carrying the credential (e.g. X-Api-Key, api_key). "
        "None for bearer/basic (standard Authorization header).",
    )

    model_config = {"extra": "forbid"}


class APIEndpoint(BaseModel):
    """One endpoint the workflow calls."""

    url_template: str = Field(
        ..., min_length=1, description="Absolute URL with {placeholders} for paging/params."
    )
    probe_url: str = Field(
        ...,
        min_length=1,
        description="A CONCRETE, immediately-callable instance of url_template (sans secret) — "
        "the validator re-calls exactly this URL to ground-truth the workflow's output.",
    )
    method: str = Field(default="GET", description="HTTP method (GET / POST / ...).")
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Free-form notes per template placeholder/query param (meaning, range, defaults).",
    )
    auth: APIEndpointAuth | None = Field(
        default=None, description="Auth this endpoint needs; None = unauthenticated."
    )
    record_path: str | None = Field(
        default=None,
        description="Dot-path from the response root to the record list (e.g. 'data.items'); "
        "None = response root is the list, or the validator's unwrap heuristics apply.",
    )
    notes: str = Field(
        default="", description="One-line gotcha: rate limits seen, quirks, pagination stop."
    )

    model_config = {"extra": "forbid"}


class APIFieldDecl(BaseModel):
    """One declared output field (== a key of output_sample.json records)."""

    name: str = Field(..., min_length=1, description="Canonical output field name.")
    semantic: str = Field(
        default="", description="What the field means and which response key(s) it comes from."
    )
    stability: FieldStabilityClass | None = Field(
        default=None,
        description="Drift class for compare_output: STRICT (exact), TOLERANT (numeric ±5/5%), "
        "SKIP (not compared). None = validator-unclassified.",
    )

    model_config = {"extra": "forbid"}


class APIManifestFile(BaseModel):
    """Top-level shape of workspaces/<site>/api_manifest.yaml."""

    workflow_type: Literal["api"] = "api"
    source_url: str = Field(
        ..., description="The API's base/source URL (provider root or API base)."
    )
    docs_url: str | None = Field(
        default=None, description="Documentation page the endpoints came from."
    )
    identifier_field: str | None = Field(
        default=None,
        description="Output field that uniquely identifies a record (drives compare_output / probe matching).",
    )
    endpoints: list[APIEndpoint] = Field(
        ..., min_length=1, description="One entry per endpoint called."
    )
    fields: list[APIFieldDecl] = Field(
        default_factory=list,
        description="Declared output fields (should mirror output_sample.json keys).",
    )
    pagination: str = Field(default="", description="Paging mechanism + stop condition, in prose.")
    rate_limit: str = Field(
        default="", description="Observed/documented rate limit + the workflow's pacing."
    )
    credentials_source: str = Field(
        default="",
        description="WHERE the secret is read from (env var name / credentials.json walk-up). "
        "NEVER a secret value.",
    )
    notes: str = Field(default="", description="Free notes.")

    model_config = {"extra": "forbid"}

    @classmethod
    def load(cls, path: Path) -> "APIManifestFile":
        if not path.exists():
            raise FileNotFoundError(
                f"api_manifest.yaml not found at {path}. An API workflow must write it "
                f"via the api_manifest_write MCP tool."
            )
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"api_manifest.yaml at {path} is unparseable YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"api_manifest.yaml at {path} has wrong top-level shape: "
                f"got {type(data).__name__}, expected mapping."
            )
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            self.model_dump(),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=100,
        )
        header = (
            "# workspaces/<site>/api_manifest.yaml — managed by mcp_server.schemas.APIManifestFile\n"
            "# Declares the endpoint(s) an API workflow.py calls + the output fields; the validator\n"
            "# re-calls probe_url and verifies the output against the live response.\n"
            "# Mutually exclusive with selectors.yaml (extraction) and download_manifest.yaml (download).\n"
            "# Its presence = api workflow_type. credentials_source NEVER holds a secret value.\n"
            "\n"
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(header + text, encoding="utf-8")
        tmp.replace(path)


__all__ = ["APIEndpointAuth", "APIEndpoint", "APIFieldDecl", "APIManifestFile"]
