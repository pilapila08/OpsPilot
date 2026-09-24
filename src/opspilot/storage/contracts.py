"""Repository protocols for V1 runtime records."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

import json

from pydantic import Field, JsonValue, field_validator, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.budget import BudgetStopReason
from opspilot.evidence.models import Contradiction, Evidence, MissingEvidence
from opspilot.execution.repository import ExecutionRepository
from opspilot.llm.audit import ModelAuditRepository
from opspilot.integrations.kubernetes.models import NamespaceName

_ID = r"^[a-z][a-z0-9_-]{2,127}$"


class RuntimePersistenceError(RuntimeError):
    """A sanitized runtime repository failure."""


class TaskSnapshot(StrictSchema):
    task_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    user_query: str = Field(min_length=1, max_length=4_000)
    namespace: NamespaceName | None = None
    status: AgentStatus = AgentStatus.CREATED
    mode: Literal["live", "replay"] | None = None
    case_id: str | None = None
    idempotency_key: str | None = None


class RunSnapshot(StrictSchema):
    run_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    task_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    attempt_no: int = Field(default=1, ge=1)
    runtime_version: Literal["v1", "v2"] = "v1"
    state: AgentState

    @model_validator(mode="after")
    def match_state(self) -> RunSnapshot:
        if self.task_id != self.state.task_id:
            raise ValueError("Run task ID must match AgentState")
        return self


class BudgetStopSnapshot(StrictSchema):
    run_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    trace_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    reason: BudgetStopReason
    round_no: int | None = Field(default=None, ge=1, le=4)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("budget stop timestamp must be timezone-aware")
        return value.astimezone(UTC)


class BudgetVerificationV3(StrictSchema):
    """Explicit no-Evidence verification for a budget-limited Partial result."""

    schema_version: Literal[3] = 3
    kind: Literal["budget_stop"] = "budget_stop"
    supported: Literal[False] = False
    checked_evidence_ids: tuple[str, ...] = ()
    missing_evidence: tuple[MissingEvidence, ...] = Field(min_length=1)
    contradictions: tuple[Contradiction, ...] = Field(default=(), max_length=0)
    rationale: str = Field(min_length=1, max_length=4_000)


class ResultSnapshot(StrictSchema):
    result_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    task_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    run_id: str = Field(min_length=3, max_length=128, pattern=_ID)
    status: Literal["COMPLETED", "PARTIAL"]
    root_cause: str = Field(min_length=1, max_length=4_000)
    recommendation: str = Field(min_length=1, max_length=4_000)
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    claims_payload: tuple[dict[str, JsonValue], ...] = ()
    verification_payload: dict[str, JsonValue]
    schema_version: Literal[1, 2, 3] = 1

    @model_validator(mode="after")
    def validate_budget_partial(self) -> ResultSnapshot:
        if self.schema_version == 3:
            if self.status != "PARTIAL" or self.claims_payload:
                raise ValueError("V3 budget result must be Partial without claims")
            BudgetVerificationV3.model_validate_json(
                json.dumps(self.verification_payload)
            )
        return self


@runtime_checkable
class TaskRepository(Protocol):
    def create_task(self, task: TaskSnapshot) -> None: ...

    def create_or_get_task(self, task: TaskSnapshot) -> tuple[TaskSnapshot, bool]: ...

    def get_by_idempotency_key(self, key: str) -> TaskSnapshot | None: ...

    def fail_unstarted_task(self, task_id: str) -> None: ...

    def get_task(self, task_id: str) -> TaskSnapshot | None: ...


@runtime_checkable
class RunRepository(Protocol):
    def create_run(self, run: RunSnapshot) -> None: ...

    def get_run(self, run_id: str) -> RunSnapshot | None: ...

    def save_state(self, run_id: str, state: AgentState) -> None: ...

    def append_budget_stop(self, stop: BudgetStopSnapshot) -> None: ...

    def budget_stops_for_trace(self, trace_id: str) -> tuple[BudgetStopSnapshot, ...]: ...


@runtime_checkable
class LlmCallRepository(ModelAuditRepository, Protocol):
    """Existing prompt and model-call audit contract."""


@runtime_checkable
class ToolCallRepository(ExecutionRepository, Protocol):
    """Append a Tool attempt together with its Evidence atomically."""


@runtime_checkable
class EvidenceRepository(Protocol):
    def evidence_for_trace(self, trace_id: str) -> tuple[Evidence, ...]: ...


@runtime_checkable
class ResultRepository(Protocol):
    def append_result(self, result: ResultSnapshot) -> None: ...

    def get_result(self, run_id: str) -> ResultSnapshot | None: ...

    def get_result_for_trace(self, trace_id: str) -> ResultSnapshot | None: ...
