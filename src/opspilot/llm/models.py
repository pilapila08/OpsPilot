"""Provider-neutral contracts for structured model generation."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Generic, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from opspilot.agent.schemas import StrictSchema

OutputT = TypeVar("OutputT", bound=BaseModel)

_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"


class ModelRole(StrEnum):
    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"


class ModelMessage(StrictSchema):
    model_config = ConfigDict(str_strip_whitespace=False)

    role: ModelRole
    content: str = Field(min_length=1, max_length=50_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model message content cannot be blank")
        return value


class StructuredModelConfig(StrictSchema):
    model_config = ConfigDict(protected_namespaces=())

    provider: str = Field(min_length=1, max_length=64, pattern=_NAME_PATTERN)
    model: str = Field(min_length=1, max_length=128)
    model_version: str | None = Field(default=None, max_length=64)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int = Field(default=1_024, ge=1, le=100_000)
    input_cost_per_million_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        max_digits=12,
        decimal_places=6,
    )
    output_cost_per_million_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        max_digits=12,
        decimal_places=6,
    )


class PromptTemplate(StrictSchema):
    model_config = ConfigDict(
        protected_namespaces=(),
        str_strip_whitespace=False,
    )

    component: str = Field(min_length=1, max_length=64, pattern=_NAME_PATTERN)
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=_VERSION_PATTERN,
    )
    content: str = Field(min_length=1, max_length=50_000)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_family: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def verify_content_hash(self) -> PromptTemplate:
        expected = sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_hash != expected:
            raise ValueError("prompt content hash does not match content")
        return self


class PromptReference(StrictSchema):
    component: str = Field(min_length=1, max_length=64, pattern=_NAME_PATTERN)
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=_VERSION_PATTERN,
    )
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def from_template(cls, prompt: PromptTemplate) -> PromptReference:
        return cls(
            component=prompt.component,
            version=prompt.version,
            content_hash=prompt.content_hash,
        )


class StructuredModelRequest(StrictSchema):
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=20)
    prompt: PromptReference
    config: StructuredModelConfig


class ModelUsage(StrictSchema):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        max_digits=12,
        decimal_places=6,
    )

    @model_validator(mode="after")
    def validate_total(self) -> ModelUsage:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input plus output tokens")
        return self


class ModelResponseMetadata(StrictSchema):
    model_config = ConfigDict(protected_namespaces=())

    provider: str = Field(min_length=1, max_length=64, pattern=_NAME_PATTERN)
    model: str = Field(min_length=1, max_length=128)
    model_version: str | None = Field(default=None, max_length=64)
    response_id: str | None = Field(default=None, max_length=128)
    latency_ms: int = Field(ge=0)
    usage: ModelUsage


class StructuredModelResult(StrictSchema, Generic[OutputT]):
    output: OutputT
    metadata: ModelResponseMetadata


class ScriptedModelResponse(StrictSchema):
    model_config = ConfigDict(protected_namespaces=())

    payload: dict[str, JsonValue]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        max_digits=12,
        decimal_places=6,
    )
    latency_ms: int = Field(default=0, ge=0)
    response_id: str | None = Field(default=None, max_length=128)
    model_version: str | None = Field(default=None, max_length=64)
