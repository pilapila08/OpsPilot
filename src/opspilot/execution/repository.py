"""Atomic Tool attempt and Evidence repository contracts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode
from opspilot.evidence.models import Evidence
from opspilot.execution.models import ToolAttempt


class ExecutionPersistenceError(RuntimeError):
    """A sanitized failure while committing one Tool attempt and its Evidence."""


class RecordedToolAttempt(StrictSchema):
    record_id: str = Field(min_length=3, max_length=128)
    run_id: str = Field(min_length=3, max_length=128)
    sequence_no: int = Field(ge=1)


class ToolAttemptView(StrictSchema):
    record_id: str
    run_id: str
    sequence_no: int = Field(ge=1)
    logical_call_id: str
    attempt_no: int = Field(ge=1)
    tool_name: str
    success: bool
    error_code: ErrorCode | None


@runtime_checkable
class ExecutionRepository(Protocol):
    def append_attempt(
        self,
        attempt: ToolAttempt,
        evidence: tuple[Evidence, ...],
    ) -> RecordedToolAttempt: ...

    def attempts_for_trace(self, trace_id: str) -> tuple[ToolAttemptView, ...]: ...

    def evidence_for_trace(self, trace_id: str) -> tuple[Evidence, ...]: ...


class InMemoryExecutionRepository:
    """Deterministic atomic sink for Executor tests."""

    def __init__(self) -> None:
        self.attempts: list[ToolAttempt] = []
        self.evidence: list[Evidence] = []

    def append_attempt(
        self,
        attempt: ToolAttempt,
        evidence: tuple[Evidence, ...],
    ) -> RecordedToolAttempt:
        _check_evidence(attempt, evidence)
        if any(item.record_id == attempt.record_id for item in self.attempts):
            raise ExecutionPersistenceError("Tool attempt ID already exists")
        if any(
            item.run_id == attempt.run_id
            and item.invocation.call_id == attempt.invocation.call_id
            and item.attempt_no == attempt.attempt_no
            for item in self.attempts
        ):
            raise ExecutionPersistenceError("logical Tool attempt already exists")
        existing_ids = {item.evidence_id for item in self.evidence}
        incoming_ids = {item.evidence_id for item in evidence}
        if len(incoming_ids) != len(evidence) or existing_ids & incoming_ids:
            raise ExecutionPersistenceError("Evidence ID already exists")
        sequence_no = sum(item.run_id == attempt.run_id for item in self.attempts) + 1
        self.attempts.append(attempt)
        self.evidence.extend(evidence)
        return RecordedToolAttempt(
            record_id=attempt.record_id,
            run_id=attempt.run_id,
            sequence_no=sequence_no,
        )

    def attempts_for_trace(self, trace_id: str) -> tuple[ToolAttemptView, ...]:
        return tuple(
            ToolAttemptView(
                record_id=item.record_id,
                run_id=item.run_id,
                sequence_no=index,
                logical_call_id=item.invocation.call_id,
                attempt_no=item.attempt_no,
                tool_name=item.invocation.tool,
                success=item.response.success,
                error_code=item.response.error.code if item.response.error is not None else None,
            )
            for index, item in enumerate(
                (attempt for attempt in self.attempts if attempt.trace_id == trace_id),
                start=1,
            )
        )

    def evidence_for_trace(self, trace_id: str) -> tuple[Evidence, ...]:
        return tuple(item for item in self.evidence if item.trace_id == trace_id)


def _check_evidence(attempt: ToolAttempt, evidence: tuple[Evidence, ...]) -> None:
    if evidence and not attempt.response.success:
        raise ExecutionPersistenceError("failed Tool attempt cannot create Evidence")
    if any(
        item.trace_id != attempt.trace_id
        or item.tool_call_id != attempt.record_id
        or item.raw_result_ref != attempt.record_id
        for item in evidence
    ):
        raise ExecutionPersistenceError("Evidence does not belong to Tool attempt")
