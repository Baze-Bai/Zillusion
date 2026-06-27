"""Per-query cost tracking across all LLM and API calls."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class StageMetrics:
    name: str
    start_time: float = 0.0
    end_time: float = 0.0
    cost_usd: float = 0.0
    llm_calls: int = 0
    api_calls: int = 0
    errors: int = 0

    @property
    def duration_ms(self) -> float:
        if self.end_time and self.start_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0


@dataclass
class CostTracker:
    """Accumulates cost and timing across a single discovery query."""

    stages: dict[str, StageMetrics] = field(default_factory=dict)
    total_cost: float = 0.0
    total_llm_calls: int = 0
    total_api_calls: int = 0
    query_start: float = field(default_factory=time.monotonic)

    def start_stage(self, name: str) -> None:
        self.stages[name] = StageMetrics(name=name, start_time=time.monotonic())

    def end_stage(self, name: str) -> None:
        if name in self.stages:
            self.stages[name].end_time = time.monotonic()

    def add_llm_cost(self, stage: str, cost: float) -> None:
        self.total_cost += cost
        self.total_llm_calls += 1
        if stage in self.stages:
            self.stages[stage].cost_usd += cost
            self.stages[stage].llm_calls += 1

    def add_api_call(self, stage: str, cost: float = 0.0) -> None:
        self.total_cost += cost
        self.total_api_calls += 1
        if stage in self.stages:
            self.stages[stage].cost_usd += cost
            self.stages[stage].api_calls += 1

    def add_error(self, stage: str) -> None:
        if stage in self.stages:
            self.stages[stage].errors += 1

    @property
    def total_duration_seconds(self) -> float:
        return time.monotonic() - self.query_start

    def summary(self) -> dict:
        return {
            "total_cost_usd": round(self.total_cost, 6),
            "total_duration_s": round(self.total_duration_seconds, 2),
            "total_llm_calls": self.total_llm_calls,
            "total_api_calls": self.total_api_calls,
            "stages": {
                name: {
                    "duration_ms": round(m.duration_ms, 1),
                    "cost_usd": round(m.cost_usd, 6),
                    "llm_calls": m.llm_calls,
                    "api_calls": m.api_calls,
                    "errors": m.errors,
                }
                for name, m in self.stages.items()
            },
        }
