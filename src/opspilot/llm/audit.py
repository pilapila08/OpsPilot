"""Audit contracts for versioned prompts and model call attempts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode
from opspilot.llm.models import PromptTemplate

_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"


class ModelAuditError(RuntimeError):
    """Raised when a model audit record cannot be persisted safely."""


class ModelCallAttempt(StrictSchema):
    model_config = ConfigDict(protected_namespaces=())

    run_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    component: str = Field(min_length=1, max_length=64)
    prompt_version_id: str = Field(min_length=3, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str | None = Field(default=None, max_length=64)
    request_payload: dict[str, JsonValue]
    response_payload: dict[str, JsonValue] | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        max_digits=12,
        decimal_places=6,
    )
    latency_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    success: bool
    error_code: ErrorCode | None = None
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("model audit timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_result(self) -> ModelCallAttempt:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.success and (
            self.response_payload is None or self.error_code is not None
        ):
            raise ValueError("successful attempts require response and no error")
        if not self.success and self.error_code is None:
            raise ValueError("failed attempts require error_code")
        return self


class RecordedModelCall(StrictSchema):
    call_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    run_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    sequence_no: int = Field(ge=1)


class ModelCallView(StrictSchema):
    """Safe, ordered read projection of one persisted model attempt."""

    model_config = ConfigDict(protected_namespaces=())

    call_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    run_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    sequence_no: int = Field(ge=1)
    component: str = Field(min_length=1, max_length=64)
    prompt_version_id: str = Field(min_length=3, max_length=128)
    round_no: int | None = Field(default=None, ge=1, le=4)
    provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str | None = Field(default=None, max_length=64)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=Decimal("0"))
    latency_ms: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    success: bool
    error_code: ErrorCode | None
    started_at: datetime
    completed_at: datetime | None

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("model call timestamps must be timezone-aware")
        return value.astimezone(UTC)


@runtime_checkable
class ModelAuditRepository(Protocol):
    def register_prompt(self, prompt: PromptTemplate) -> str: ...

    def append_attempt(
        self,
        attempt: ModelCallAttempt,
    ) -> RecordedModelCall: ...

    def calls_for_trace(self, trace_id: str) -> tuple[ModelCallView, ...]: ...


class InMemoryModelAuditRepository:
    """Deterministic audit sink for Router and Planner unit tests."""

    def __init__(self, trace_by_run: Mapping[str, str] | None = None) -> None:
        self.prompts: dict[tuple[str, str], PromptTemplate] = {}
        self.attempts: list[ModelCallAttempt] = []
        self.trace_by_run: dict[str, str] = dict(trace_by_run or {})

    def bind_run_trace(self, run_id: str, trace_id: str) -> None:
        """Supply the Run/Trace identity absent from ModelCallAttempt."""
        existing = self.trace_by_run.get(run_id)
        if existing is not None and existing != trace_id:
            raise ModelAuditError("run is already bound to another trace")
        self.trace_by_run[run_id] = trace_id

    def register_prompt(self, prompt: PromptTemplate) -> str:
        key = (prompt.component, prompt.version)
        existing = self.prompts.get(key)
        if existing is not None and existing != prompt:
            raise ModelAuditError(
                "prompt version already exists with different content"
            )
        self.prompts[key] = prompt
        return _prompt_id(prompt)

    def append_attempt(
        self,
        attempt: ModelCallAttempt,
    ) -> RecordedModelCall:
        sequence_no = (
            sum(item.run_id == attempt.run_id for item in self.attempts) + 1
        )
        self.attempts.append(attempt)
        return RecordedModelCall(
            call_id=f"llm_{attempt.run_id}_{sequence_no}",
            run_id=attempt.run_id,
            sequence_no=sequence_no,
        )

    def calls_for_trace(self, trace_id: str) -> tuple[ModelCallView, ...]:
        sequence_by_run: dict[str, int] = {}
        views: list[ModelCallView] = []
        for attempt in self.attempts:
            sequence_no = sequence_by_run.get(attempt.run_id, 0) + 1
            sequence_by_run[attempt.run_id] = sequence_no
            if self.trace_by_run.get(attempt.run_id) != trace_id:
                continue
            views.append(ModelCallView(
                call_id=f"llm_{attempt.run_id}_{sequence_no}",
                run_id=attempt.run_id,
                sequence_no=sequence_no,
                component=attempt.component,
                prompt_version_id=attempt.prompt_version_id,
                round_no=safe_round_no(attempt.request_payload),
                provider=attempt.provider,
                model_name=attempt.model_name,
                model_version=attempt.model_version,
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                cost_usd=attempt.cost_usd,
                latency_ms=attempt.latency_ms,
                retry_count=attempt.retry_count,
                success=attempt.success,
                error_code=attempt.error_code,
                started_at=attempt.started_at,
                completed_at=attempt.completed_at,
            ))
        return tuple(views)


def safe_round_no(payload: Mapping[str, object]) -> int | None:
    """Project only a valid V2 round number from stored request metadata."""
    value = payload.get("round_no")
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 4 else None


def prompt_record_id(prompt: PromptTemplate) -> str:
    return _prompt_id(prompt)


def _prompt_id(prompt: PromptTemplate) -> str:
    return (
        f"prompt_{prompt.component}_{prompt.version}_"
        f"{prompt.content_hash[:12]}"
    )
