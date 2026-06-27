"""Source routing models for Stage 2."""

from pydantic import BaseModel, Field


class ActivatedWorkerSet(BaseModel):
    """Output of Source Router — which workers to activate and their priorities."""

    workers: list[str] = Field(
        default_factory=lambda: ["web"],
        description="Activated worker names, 'web' always included",
    )
    priorities: dict[str, int] = Field(
        default_factory=dict,
        description="Worker name -> priority (higher = more important)",
    )
    llm_additions: list[str] = Field(
        default_factory=list,
        description="Workers added by LLM supplement layer",
    )
    llm_removals: list[str] = Field(
        default_factory=list,
        description="Workers removed by LLM supplement layer",
    )
    llm_reason: str = ""
