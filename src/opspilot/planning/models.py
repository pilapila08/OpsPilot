"""Validated plan and planner result contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import ExecutionPlanV1, StrictSchema
from opspilot.agent.state import AgentState


class ValidatedPlanV1(StrictSchema):
    plan_json: str = Field(min_length=1, max_length=50_000)
    tool_versions: tuple[tuple[str, str], ...]

    @field_validator("plan_json")
    @classmethod
    def require_valid_plan(cls, value: str) -> str:
        ExecutionPlanV1.model_validate_json(value)
        return value

    @model_validator(mode="after")
    def require_matching_tools(self) -> ValidatedPlanV1:
        steps = self.plan.steps
        if len(self.tool_versions) != len(steps) or any(
            name != step.tool
            for (name, _), step in zip(self.tool_versions, steps, strict=True)
        ):
            raise ValueError("validated tool versions must match plan steps")
        return self

    @property
    def plan(self) -> ExecutionPlanV1:
        return ExecutionPlanV1.model_validate_json(self.plan_json)

    @classmethod
    def from_plan(
        cls,
        plan: ExecutionPlanV1,
        tool_versions: tuple[tuple[str, str], ...],
    ) -> ValidatedPlanV1:
        return cls(plan_json=plan.model_dump_json(), tool_versions=tool_versions)


class PlannerSettings(StrictSchema):
    schema_version: Literal[1] = 1
    max_schema_retries: int = Field(default=2, ge=0, le=2)
    max_plan_retries: int = Field(default=1, ge=0, le=1)


class PlannerOutcome(StrictSchema):
    state: AgentState
    validated_plan: ValidatedPlanV1
    attempts: int = Field(ge=1, le=4)
    llm_call_ids: tuple[str, ...] = Field(min_length=1, max_length=4)
