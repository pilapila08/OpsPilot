"""SQLAlchemy transaction boundary for Tool attempts and their Evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opspilot.evidence.models import Evidence, EvidenceAttribute
from opspilot.errors import ErrorCode
from opspilot.execution.models import ToolAttempt
from opspilot.execution.repository import (
    ExecutionPersistenceError,
    RecordedToolAttempt,
    ToolAttemptView,
    _check_evidence,
)
from opspilot.storage.models import AgentRunRecord, EvidenceRecord, ToolCallRecord


class SQLAlchemyExecutionRepository:
    """Commit a Tool attempt and zero or more Evidence rows as one unit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_attempt(
        self,
        attempt: ToolAttempt,
        evidence: tuple[Evidence, ...],
    ) -> RecordedToolAttempt:
        _check_evidence(attempt, evidence)
        try:
            run = self._session.get(AgentRunRecord, attempt.run_id)
            if run is None or run.trace_id != attempt.trace_id:
                raise ExecutionPersistenceError("Tool attempt run and trace do not match")
            current_sequence = self._session.scalar(
                select(func.max(ToolCallRecord.sequence_no)).where(
                    ToolCallRecord.run_id == attempt.run_id
                )
            )
            sequence_no = int(current_sequence or 0) + 1
            record = ToolCallRecord(
                id=attempt.record_id,
                run_id=attempt.run_id,
                sequence_no=sequence_no,
                logical_call_id=attempt.invocation.call_id,
                attempt_no=attempt.attempt_no,
                tool_name=attempt.invocation.tool,
                tool_version=attempt.response.metadata.tool_version,
                risk_level=int(attempt.risk_level),
                arguments_payload=dict(attempt.invocation.arguments),
                result_payload=attempt.response.model_dump(mode="json"),
                duration_ms=attempt.response.metadata.duration_ms,
                success=attempt.response.success,
                error_code=(
                    attempt.response.error.code.value
                    if attempt.response.error is not None
                    else None
                ),
                started_at=attempt.started_at,
                completed_at=attempt.completed_at,
            )
            self._session.add(record)
            for item in evidence:
                self._session.add(_evidence_record(item, attempt.run_id))
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            raise ExecutionPersistenceError(
                "Tool attempt and Evidence could not be persisted"
            ) from None
        return RecordedToolAttempt(
            record_id=attempt.record_id,
            run_id=attempt.run_id,
            sequence_no=sequence_no,
        )

    def attempts_for_trace(self, trace_id: str) -> tuple[ToolAttemptView, ...]:
        rows = self._session.scalars(
                select(ToolCallRecord)
                .join(AgentRunRecord, ToolCallRecord.run_id == AgentRunRecord.id)
                .where(AgentRunRecord.trace_id == trace_id)
                .order_by(ToolCallRecord.sequence_no)
            ).all()
        return tuple(
            ToolAttemptView(
                record_id=row.id,
                run_id=row.run_id,
                sequence_no=row.sequence_no,
                logical_call_id=row.logical_call_id,
                attempt_no=row.attempt_no,
                tool_name=row.tool_name,
                success=bool(row.success),
                error_code=ErrorCode(row.error_code) if row.error_code else None,
            )
            for row in rows
        )

    def evidence_for_trace(self, trace_id: str) -> tuple[Evidence, ...]:
        rows = self._session.scalars(
                select(EvidenceRecord)
                .join(AgentRunRecord, EvidenceRecord.run_id == AgentRunRecord.id)
                .where(AgentRunRecord.trace_id == trace_id)
                .order_by(EvidenceRecord.created_at, EvidenceRecord.id)
            ).all()
        return tuple(
            Evidence(
                evidence_id=row.id,
                trace_id=trace_id,
                tool_call_id=row.tool_call_id,
                source=row.source,
                resource=row.resource,
                observed_at=_aware(row.observed_at),
                collected_at=_aware(row.collected_at),
                content=row.content,
                source_confidence=float(row.source_confidence),
                raw_result_ref=row.raw_result_ref,
                attributes=tuple(EvidenceAttribute.model_validate(item) for item in row.attributes_payload),
            )
            for row in rows
        )


def _evidence_record(item: Evidence, run_id: str) -> EvidenceRecord:
    attributes: list[dict[str, Any]] = [
        attribute.model_dump(mode="json") for attribute in item.attributes
    ]
    return EvidenceRecord(
        id=item.evidence_id,
        run_id=run_id,
        tool_call_id=item.tool_call_id,
        source=item.source,
        resource=item.resource,
        observed_at=item.observed_at,
        collected_at=item.collected_at,
        content=item.content,
        source_confidence=Decimal(str(item.source_confidence)),
        raw_result_ref=item.raw_result_ref,
        attributes_payload=attributes,
        schema_version=1,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
