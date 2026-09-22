"""Audit contracts for versioned prompts and model call attempts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
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


@runtime_checkable
class ModelAuditRepository(Protocol):
    def register_prompt(self, prompt: PromptTemplate) -> str: ...

    def append_attempt(
        self,
        attempt: ModelCallAttempt,
    ) -> RecordedModelCall: ...


class InMemoryModelAuditRepository:
    """Deterministic audit sink for Router and Planner unit tests."""

    def __init__(self) -> None:
        self.prompts: dict[tuple[str, str], PromptTemplate] = {}
        self.attempts: list[ModelCallAttempt] = []

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


def prompt_record_id(prompt: PromptTemplate) -> str:
    return _prompt_id(prompt)


def _prompt_id(prompt: PromptTemplate) -> str:
    return (
        f"prompt_{prompt.component}_{prompt.version}_"
        f"{prompt.content_hash[:12]}"
    )
