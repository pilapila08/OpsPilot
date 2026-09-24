"""SQLAlchemy persistence for prompt versions and every model attempt."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opspilot.llm.audit import (
    ModelAuditError,
    ModelCallAttempt,
    ModelCallView,
    RecordedModelCall,
    prompt_record_id,
    safe_round_no,
)
from opspilot.errors import ErrorCode
from opspilot.llm.models import PromptTemplate
from opspilot.storage.models import AgentRunRecord, LLMCallRecord, PromptVersionRecord


class SQLAlchemyModelAuditRepository:
    """Commit prompt and LLM call audit rows through an explicit repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def register_prompt(self, prompt: PromptTemplate) -> str:
        existing = self._session.scalar(
            select(PromptVersionRecord).where(
                PromptVersionRecord.component == prompt.component,
                PromptVersionRecord.version == prompt.version,
            )
        )
        if existing is not None:
            if (
                existing.content_hash != prompt.content_hash
                or existing.template_text != prompt.content
                or existing.model_family != prompt.model_family
            ):
                raise ModelAuditError(
                    "prompt version already exists with different content"
                )
            return existing.id

        record = PromptVersionRecord(
            id=prompt_record_id(prompt),
            component=prompt.component,
            version=prompt.version,
            content_hash=prompt.content_hash,
            template_text=prompt.content,
            model_family=prompt.model_family,
        )
        self._session.add(record)
        self._commit("prompt version")
        return record.id

    def append_attempt(
        self,
        attempt: ModelCallAttempt,
    ) -> RecordedModelCall:
        current_sequence = self._session.scalar(
            select(func.max(LLMCallRecord.sequence_no)).where(
                LLMCallRecord.run_id == attempt.run_id
            )
        )
        sequence_no = int(current_sequence or 0) + 1
        call_id = f"llm_{uuid4().hex}"
        record = LLMCallRecord(
            id=call_id,
            run_id=attempt.run_id,
            prompt_version_id=attempt.prompt_version_id,
            sequence_no=sequence_no,
            component=attempt.component,
            provider=attempt.provider,
            model_name=attempt.model_name,
            model_version=attempt.model_version,
            request_payload=_json_dict(attempt.request_payload),
            response_payload=(
                _json_dict(attempt.response_payload)
                if attempt.response_payload is not None
                else None
            ),
            input_tokens=attempt.input_tokens,
            output_tokens=attempt.output_tokens,
            cost_usd=attempt.cost_usd,
            latency_ms=attempt.latency_ms,
            retry_count=attempt.retry_count,
            success=attempt.success,
            error_code=(
                attempt.error_code.value
                if attempt.error_code is not None
                else None
            ),
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
        )
        self._session.add(record)
        self._commit("model call")
        return RecordedModelCall(
            call_id=call_id,
            run_id=attempt.run_id,
            sequence_no=sequence_no,
        )

    def calls_for_trace(self, trace_id: str) -> tuple[ModelCallView, ...]:
        rows = self._session.scalars(
            select(LLMCallRecord)
            .join(AgentRunRecord, LLMCallRecord.run_id == AgentRunRecord.id)
            .where(AgentRunRecord.trace_id == trace_id)
            .order_by(LLMCallRecord.sequence_no, LLMCallRecord.id)
        ).all()
        return tuple(ModelCallView(
            call_id=row.id,
            run_id=row.run_id,
            sequence_no=row.sequence_no,
            component=row.component,
            prompt_version_id=row.prompt_version_id,
            round_no=safe_round_no(row.request_payload),
            provider=row.provider,
            model_name=row.model_name,
            model_version=row.model_version,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=row.cost_usd,
            latency_ms=row.latency_ms,
            retry_count=row.retry_count,
            success=row.success,
            error_code=ErrorCode(row.error_code) if row.error_code else None,
            started_at=_aware(row.started_at),
            completed_at=_aware(row.completed_at) if row.completed_at else None,
        ) for row in rows)

    def _commit(self, record_type: str) -> None:
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            raise ModelAuditError(
                f"{record_type} audit record could not be persisted"
            ) from None


def _json_dict(value: Mapping[str, object]) -> dict[str, Any]:
    return dict(value)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
