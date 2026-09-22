"""Type-safe contracts for registered OpsPilot tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from inspect import iscoroutinefunction
from typing import Generic, TypeVar

from pydantic import BaseModel, Field, JsonValue, ValidationError, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode, ErrorInfo, error_policy

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
_CALL_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
_VERSION_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,31}$"
_SOURCE_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"


class ToolRiskLevel(IntEnum):
    """Policy risk assigned to a tool definition."""

    READ_ONLY = 0
    APPROVAL_REQUIRED = 1
    PROHIBITED = 2


class RetryPolicy(StrictSchema):
    """Retry declaration; retries are executed by a later Executor stage."""

    max_retries: int = Field(default=0, ge=0, le=10)
    initial_backoff_seconds: float = Field(default=0.25, ge=0, le=60)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)
    max_backoff_seconds: float = Field(default=10, ge=0, le=300)
    retryable_errors: tuple[ErrorCode, ...] = (
        ErrorCode.TOOL_TIMEOUT,
        ErrorCode.TOOL_EXECUTION_FAILED,
    )

    @model_validator(mode="after")
    def validate_backoff_range(self) -> RetryPolicy:
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError(
                "initial_backoff_seconds cannot exceed max_backoff_seconds"
            )
        if len(set(self.retryable_errors)) != len(self.retryable_errors):
            raise ValueError("retryable_errors cannot contain duplicates")
        if any(not error_policy(code).retryable for code in self.retryable_errors):
            raise ValueError("retry policy cannot promote non-retryable errors")
        if self.max_retries > 0 and self.retryable_errors:
            allowed_retries = min(
                error_policy(code).max_retries for code in self.retryable_errors
            )
            if self.max_retries > allowed_retries:
                raise ValueError("tool retries cannot exceed taxonomy defaults")
        return self

    def permits(self, code: ErrorCode) -> bool:
        return self.max_retries > 0 and code in self.retryable_errors


class ToolInvocation(StrictSchema):
    """A planner request to invoke one registered tool."""

    call_id: str = Field(min_length=3, max_length=128, pattern=_CALL_ID_PATTERN)
    tool: str = Field(min_length=3, max_length=128, pattern=TOOL_NAME_PATTERN)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolMetadata(StrictSchema):
    call_id: str = Field(min_length=3, max_length=128, pattern=_CALL_ID_PATTERN)
    tool_name: str = Field(min_length=3, max_length=128, pattern=TOOL_NAME_PATTERN)
    source: str = Field(min_length=1, max_length=64, pattern=_SOURCE_PATTERN)
    duration_ms: int = Field(ge=0)
    tool_version: str = Field(
        min_length=1,
        max_length=32,
        pattern=_VERSION_PATTERN,
    )


class ToolResponse(StrictSchema):
    """Normalized response returned across the Tool Gateway boundary."""

    success: bool
    data: dict[str, JsonValue] | None
    metadata: ToolMetadata
    error: ErrorInfo | None

    @model_validator(mode="after")
    def validate_result_shape(self) -> ToolResponse:
        if self.success and (self.data is None or self.error is not None):
            raise ValueError("successful responses require data and forbid error")
        if not self.success and (self.data is not None or self.error is None):
            raise ValueError("failed responses require error and forbid data")
        return self


class ToolDescriptor(StrictSchema):
    """Serializable definition exposed to planners and audit records."""

    name: str = Field(min_length=3, max_length=128, pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=1_000)
    risk_level: ToolRiskLevel
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    timeout_seconds: float = Field(gt=0, le=300)
    retry_policy: RetryPolicy
    version: str = Field(min_length=1, max_length=32, pattern=_VERSION_PATTERN)
    source: str = Field(min_length=1, max_length=64, pattern=_SOURCE_PATTERN)


class InvalidToolDefinition(ValueError):
    """Raised when a tool cannot satisfy the registration contract."""


ToolInput = TypeVar("ToolInput", bound=BaseModel)
ToolOutput = TypeVar("ToolOutput", bound=BaseModel)
ToolHandler = Callable[[ToolInput], Awaitable[ToolOutput]]


@dataclass(frozen=True, slots=True)
class ToolDefinition(Generic[ToolInput, ToolOutput]):
    """Runtime binding between a serializable descriptor and its handler."""

    name: str
    description: str
    risk_level: ToolRiskLevel
    input_model: type[ToolInput]
    output_model: type[ToolOutput]
    handler: ToolHandler[ToolInput, ToolOutput]
    source: str
    timeout_seconds: float = 10
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    version: str = "v1"

    def __post_init__(self) -> None:
        if not isinstance(self.input_model, type) or not issubclass(
            self.input_model, BaseModel
        ):
            raise InvalidToolDefinition("input_model must be a Pydantic model")
        if not isinstance(self.output_model, type) or not issubclass(
            self.output_model, BaseModel
        ):
            raise InvalidToolDefinition("output_model must be a Pydantic model")
        if not iscoroutinefunction(self.handler):
            raise InvalidToolDefinition("tool handler must be asynchronous")

        try:
            descriptor = ToolDescriptor(
                name=self.name,
                description=self.description,
                risk_level=self.risk_level,
                input_schema=self.input_model.model_json_schema(),
                output_schema=self.output_model.model_json_schema(),
                timeout_seconds=self.timeout_seconds,
                retry_policy=self.retry_policy,
                version=self.version,
                source=self.source,
            )
        except ValidationError as exc:
            raise InvalidToolDefinition("tool definition metadata is invalid") from exc

        object.__setattr__(self, "name", descriptor.name)
        object.__setattr__(self, "description", descriptor.description)
        object.__setattr__(self, "risk_level", descriptor.risk_level)
        object.__setattr__(self, "timeout_seconds", descriptor.timeout_seconds)
        object.__setattr__(self, "retry_policy", descriptor.retry_policy)
        object.__setattr__(self, "version", descriptor.version)
        object.__setattr__(self, "source", descriptor.source)

    @property
    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=self.name,
            description=self.description,
            risk_level=self.risk_level,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            timeout_seconds=self.timeout_seconds,
            retry_policy=self.retry_policy,
            version=self.version,
            source=self.source,
        )
