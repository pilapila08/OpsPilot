"""Application-level request and result contracts for one V1 run."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.errors import ErrorInfo
from opspilot.integrations.kubernetes.models import NamespaceName
from opspilot.storage.contracts import ResultSnapshot


class DiagnosisRequest(StrictSchema):
    query: str = Field(min_length=1, max_length=4_000)
    namespace: NamespaceName
    mode: Literal["live", "replay"]
    case_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{2,127}$")

    @model_validator(mode="after")
    def validate_mode(self) -> DiagnosisRequest:
        if self.mode == "replay" and self.case_id is None:
            raise ValueError("replay requires a case_id")
        if self.mode == "live" and self.case_id is not None:
            raise ValueError("live mode cannot select a replay case")
        return self


class DiagnosisRunResult(StrictSchema):
    task_id: str = Field(min_length=3, max_length=128)
    run_id: str = Field(min_length=3, max_length=128)
    trace_id: str = Field(min_length=3, max_length=128)
    status: AgentStatus
    state: AgentState
    diagnosis: ResultSnapshot | None = None
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def match_state(self) -> DiagnosisRunResult:
        if (
            self.state.task_id != self.task_id
            or self.state.trace_id != self.trace_id
            or self.state.status is not self.status
            or not self.status.is_terminal
        ):
            raise ValueError("runtime result must match a terminal AgentState")
        if self.diagnosis is not None and (
            self.diagnosis.task_id != self.task_id
            or self.diagnosis.run_id != self.run_id
        ):
            raise ValueError("diagnosis Result belongs to another Run")
        if self.status in {AgentStatus.COMPLETED, AgentStatus.PARTIAL} and self.diagnosis is None:
            raise ValueError("completed or partial run requires a diagnosis Result")
        return self
