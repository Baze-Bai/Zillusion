"""Structured requirement — the output of Intent Parser (Stage 1)."""

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator


class StructuredRequirement(BaseModel):
    """Intent Parser output — drives all subsequent stages."""

    # === Core Need ===
    original_query: str
    # Concise sidebar/title label for this task. Synthesized by parse_intent;
    # surfaced to the UI via the session_titled event. Same language as query.
    title: str = Field(
        default="",
        description="A concise 3-8 word title naming the data need (same language as the user's query), e.g. 'US historical weather data sources'. Suitable as a sidebar label.",
    )
    domain: str = Field(description="finance / health / geo / science / tech / ...")
    sub_domains: list[str] = Field(default_factory=list)
    data_type_hints: list[str] = Field(
        default_factory=list,
        description="api / tabular / timeseries / text / geo / ...",
    )
    desired_formats: list[str] = Field(
        default_factory=list, description="csv, json, api, any"
    )

    # === Constraints ===
    geographic_scope: list[str] = Field(default_factory=list)
    temporal_range: str | None = None  # e.g. "2020-2024" or "latest"
    update_frequency: str | None = None  # realtime / daily / monthly / any

    @field_validator("temporal_range", "update_frequency", mode="before")
    @classmethod
    def _coerce_null_strings(cls, v: Any) -> Any:
        # LLMs sometimes emit literal "null"/"none"/"" instead of JSON null.
        if isinstance(v, str) and v.strip().lower() in ("null", "none", ""):
            return None
        return v
    license_constraint: str = "any"  # commercial / open_only / any
    budget_constraint: str = "any"  # free_only / freemium / any

    @field_validator("license_constraint", mode="before")
    @classmethod
    def _normalize_license_constraint(cls, v: Any) -> str:
        """Normalize parse_intent's free-form license_constraint output to
        one of {any, commercial, open_only}.

        2026-05-22: Without this, parse_intent's LLM frequently emits
        variants like "commercial use" / "open source" / "商用" /
        "research-friendly" that don't match score_license_fit's exact
        string compares and silently degrade the constraint signal to
        "any". The normalizer (in judging/deterministic.py) handles
        Chinese + English aliases and falls back to "any" safely.
        """
        # Lazy import to avoid a circular: judging imports models for
        # DataSource at runtime; pulling deterministic at module-load
        # would create a startup-time cycle.
        from src.judging.deterministic import normalize_license_constraint
        return normalize_license_constraint(v)

    # === Search Drivers ===
    sub_questions: list[str] = Field(
        default_factory=list,
        description="2-5 independently searchable sub-questions",
    )
    search_keywords_zh: list[str] = Field(
        default_factory=list, description="Chinese keywords 5-10"
    )
    search_keywords_en: list[str] = Field(
        default_factory=list, description="English keywords 5-10 (mandatory!)"
    )
    known_authoritative_sources: list[str] = Field(
        default_factory=list,
        description="LLM prior knowledge recommended authoritative sources",
    )
    target_registries: list[str] = Field(
        default_factory=list, description="Suggested registries to query"
    )

    # === User Target Schema ===
    target_schema: dict[str, Any] | None = Field(
        default=None,
        description="Expected data fields as JSON Schema, e.g. {company_name, round, amount_usd}",
    )

    @field_validator("target_schema", mode="before")
    @classmethod
    def _coerce_string_schema(cls, v: Any) -> Any:
        # GLM-4.6 (and other tool-calling LLMs) sometimes emit the
        # target_schema field as a JSON-encoded string instead of an
        # object literal — e.g. ``"target_schema": "{\"properties\": ...}"``.
        # Without this coercion, Pydantic raises ``Input should be an
        # object [type=dict_type]`` on each instructor retry, the
        # structured call fails 2 attempts on the primary tier, falls back
        # to the next tier (zai/glm-5), and parse_intent's wall-clock
        # balloons from ~10s to ~70s with no behavioural change.
        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() in ("null", "none"):
                return None
            try:
                parsed = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                return None
            if isinstance(parsed, dict):
                return parsed
            return None
        return v
