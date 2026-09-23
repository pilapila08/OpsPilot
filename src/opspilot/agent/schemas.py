"""Structured contracts shared by the first OpsPilot runtime stages."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

MAX_PLAN_STEPS = 8

_K8S_NAMESPACE_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_K8S_RESOURCE_PATTERN = (
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
)
_PROBLEM_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"


class StrictSchema(BaseModel):
    """Base model for data crossing an Agent Runtime boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class Target(StrictSchema):
    """A Kubernetes-scoped target extracted from a user request."""

    namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=_K8S_NAMESPACE_PATTERN,
    )
    resource: str = Field(
        min_length=1,
        max_length=253,
        pattern=_K8S_RESOURCE_PATTERN,
    )


class IntentOutput(StrictSchema):
    """Structured output produced by the intent router."""

    intent: Literal["diagnose"]
    domain: Literal["kubernetes", "service", "network", "deployment"]
    problem_type: str = Field(
        min_length=1,
        max_length=64,
        pattern=_PROBLEM_TYPE_PATTERN,
    )
    target: Target


class PlanStep(StrictSchema):
    """One bounded, explainable tool invocation proposed by the planner."""

    step_id: int = Field(ge=1)
    tool: str = Field(min_length=3, max_length=128, pattern=_TOOL_NAME_PATTERN)
    reason: str = Field(min_length=1, max_length=500)


class Plan(StrictSchema):
    """A validated diagnostic plan with deterministic step numbering."""

    steps: tuple[PlanStep, ...] = Field(
        min_length=1,
        max_length=MAX_PLAN_STEPS,
    )

    @model_validator(mode="after")
    def validate_step_order(self) -> Plan:
        step_ids = [step.step_id for step in self.steps]
        expected = list(range(1, len(self.steps) + 1))
        if step_ids != expected:
            raise ValueError("plan step_id values must be sequential and start at 1")
        return self


class ExecutableStepV1(StrictSchema):
    """A V1 declaration of one explicit, JSON-only tool invocation."""

    step_id: int = Field(ge=1)
    call_id: str = Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    tool: str = Field(min_length=3, max_length=128, pattern=_TOOL_NAME_PATTERN)
    arguments: dict[str, JsonValue]
    reason: str = Field(min_length=1, max_length=500)


class ExecutionPlanV1(StrictSchema):
    """Versioned executable plan, separate from the frozen V0 Plan."""

    schema_version: Literal[1]
    steps: tuple[ExecutableStepV1, ...] = Field(min_length=1, max_length=MAX_PLAN_STEPS)

    @model_validator(mode="after")
    def validate_steps(self) -> ExecutionPlanV1:
        if [step.step_id for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("plan step_id values must be sequential and start at 1")
        if len({step.call_id for step in self.steps}) != len(self.steps):
            raise ValueError("plan call_id values must be unique")
        return self


class BudgetLimits(StrictSchema):
    """Hard limits configured for one diagnosis run."""

    max_steps: int = Field(default=8, ge=1, le=32)
    max_tool_calls: int = Field(default=15, ge=1, le=100)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_tokens: int = Field(default=30_000, ge=1, le=1_000_000)
    max_cost_usd: Decimal = Field(
        default=Decimal("0.15"),
        gt=Decimal("0"),
        max_digits=10,
        decimal_places=4,
    )
    timeout_seconds: int = Field(default=90, ge=1, le=3600)


class BudgetState(StrictSchema):
    """Limits and accumulated usage for one diagnosis run."""

    limits: BudgetLimits = Field(default_factory=BudgetLimits)
    steps_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    retries_used: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        max_digits=10,
        decimal_places=4,
    )
    elapsed_seconds: float = Field(default=0, ge=0)

    @property
    def exhausted_dimensions(self) -> tuple[str, ...]:
        """Return every budget dimension that cannot accept more work."""

        exhausted: list[str] = []
        if self.steps_used >= self.limits.max_steps:
            exhausted.append("steps")
        if self.tool_calls_used >= self.limits.max_tool_calls:
            exhausted.append("tool_calls")
        if self.retries_used >= self.limits.max_retries:
            exhausted.append("retries")
        if self.tokens_used >= self.limits.max_tokens:
            exhausted.append("tokens")
        if self.cost_usd >= self.limits.max_cost_usd:
            exhausted.append("cost")
        if self.elapsed_seconds >= self.limits.timeout_seconds:
            exhausted.append("time")
        return tuple(exhausted)
