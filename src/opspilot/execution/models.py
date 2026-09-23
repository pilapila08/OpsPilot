"""Immutable records exchanged across the Executor persistence boundary."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.agent.state import AgentState
from opspilot.errors import ErrorInfo
from opspilot.tools.models import ToolInvocation, ToolResponse, ToolRiskLevel

_ID = r"^[a-z][a-z0-9_-]{2,127}$"


class ToolAttempt(StrictSchema):
    record_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    run_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    trace_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    invocation: ToolInvocation
    attempt_no: int = Field(ge=1)
    risk_level: ToolRiskLevel
    started_at: datetime
    completed_at: datetime
    response: ToolResponse

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Tool attempt time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def match_invocation(self) -> ToolAttempt:
        if self.completed_at < self.started_at:
            raise ValueError("Tool attempt completion cannot precede start")
        if self.response.metadata.call_id != self.invocation.call_id:
            raise ValueError("Tool response call ID differs from invocation")
        if self.response.metadata.tool_name != self.invocation.tool:
            raise ValueError("Tool response name differs from invocation")
        return self


class ExecutionSummary(StrictSchema):
    state: AgentState
    completed_steps: int = Field(ge=0, le=8)
    tool_attempt_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    error: ErrorInfo | None = None
