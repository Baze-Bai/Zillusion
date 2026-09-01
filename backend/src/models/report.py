"""Final report and critic output models."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, field_validator

from src.models.data_source import DataSource
from src.models.requirement import StructuredRequirement


def _coerce_str_list(value):
    """Accept list[str], JSON-encoded string, or comma-separated string.

    GLM models routinely return list-typed fields as JSON-encoded strings
    (e.g. '["a", "b"]') — this would otherwise cause a Pydantic
    validation error and force an unnecessary LLM fallback retry.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except json.JSONDecodeError:
                pass
        return [part.strip() for part in s.split(",") if part.strip()]
    return value


class CriticOutput(BaseModel):
    """Output of the Reflect & Gap Check stage (Stage 7)."""

    is_sufficient: bool = False
    coverage_analysis: str = ""
    gaps: list[str] = Field(default_factory=list)
    quality_issues: list[str] = Field(default_factory=list)
    next_round_feedback: str | None = None
    new_keywords: list[str] | None = None
    new_registries: list[str] | None = None
    new_portal_urls: list[str] | None = None

    @field_validator("gaps", "quality_issues", mode="before")
    @classmethod
    def _coerce_required_list(cls, v):
        coerced = _coerce_str_list(v)
        return coerced if coerced is not None else []

    @field_validator("new_keywords", "new_registries", "new_portal_urls", mode="before")
    @classmethod
    def _coerce_optional_list(cls, v):
        return _coerce_str_list(v)


class FinalReport(BaseModel):
    """Complete output of the discovery pipeline (Stage 8)."""

    query: str
    requirement: StructuredRequirement

    # === Three categories grouped ===
    api_sources: list[DataSource] = Field(default_factory=list)
    file_sources: list[DataSource] = Field(default_factory=list)
    embedded_sources: list[DataSource] = Field(default_factory=list)

    # === Cross-type unified ranking ===
    all_sources_ranked: list[DataSource] = Field(default_factory=list)
    total_found: int = 0
    coverage_summary: str = ""

    # === Usage Guides ===
    api_quickstart_guide: str | None = None
    file_download_guide: str | None = None
    embedded_extraction_guide: str | None = None

    # === Meta ===
    iterations: int = 1
    # (There was a `total_candidates_screened` here. It read a state key
    # `raw_candidates` that no node has written since the staged pipeline was
    # replaced by the agentic discovery node, so it reported 0 on every run —
    # which reads as "screened nothing" rather than "not measured". Removed
    # rather than defaulted: a number nobody produces is worse than no number.)
    processing_time_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
