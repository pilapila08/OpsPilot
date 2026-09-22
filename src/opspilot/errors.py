"""Unified error taxonomy and default handling policy for OpsPilot."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType

from pydantic import Field, model_validator

from opspilot.agent.schemas import StrictSchema


class ErrorCategory(StrEnum):
    LLM = "LLM"
    SCHEMA = "SCHEMA"
    TOOL = "TOOL"
    PERMISSION = "PERMISSION"
    POLICY = "POLICY"
    BUDGET = "BUDGET"
    CONTEXT = "CONTEXT"
    EXTERNAL = "EXTERNAL"


class ErrorAction(StrEnum):
    RETRY = "RETRY"
    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    REGENERATE = "REGENERATE"
    FAIL = "FAIL"
    ESCALATE_POLICY = "ESCALATE_POLICY"
    RETURN_PARTIAL = "RETURN_PARTIAL"
    REDUCE_CONTEXT = "REDUCE_CONTEXT"


class ErrorCode(StrEnum):
    LLM_RATE_LIMIT = "LLM_RATE_LIMIT"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    SCHEMA_VALIDATION = "SCHEMA_VALIDATION"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_OUTPUT_INVALID = "TOOL_OUTPUT_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    POLICY_REJECTED = "POLICY_REJECTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CONTEXT_TOO_LONG = "CONTEXT_TOO_LONG"
    EXTERNAL_SERVICE_ERROR = "EXTERNAL_SERVICE_ERROR"


class ErrorPolicy(StrictSchema):
    """Default handling policy for one stable error code."""

    category: ErrorCategory
    action: ErrorAction
    retryable: bool
    max_retries: int = Field(ge=0, le=10)

    @model_validator(mode="after")
    def validate_retry_limit(self) -> ErrorPolicy:
        if not self.retryable and self.max_retries != 0:
            raise ValueError("non-retryable policies must set max_retries to 0")
        if self.retryable and self.max_retries == 0:
            raise ValueError("retryable policies must allow at least one retry")
        return self


_ERROR_POLICIES: dict[ErrorCode, ErrorPolicy] = {
    ErrorCode.LLM_RATE_LIMIT: ErrorPolicy(
        category=ErrorCategory.LLM,
        action=ErrorAction.RETRY_WITH_BACKOFF,
        retryable=True,
        max_retries=3,
    ),
    ErrorCode.LLM_TIMEOUT: ErrorPolicy(
        category=ErrorCategory.LLM,
        action=ErrorAction.RETRY,
        retryable=True,
        max_retries=2,
    ),
    ErrorCode.SCHEMA_VALIDATION: ErrorPolicy(
        category=ErrorCategory.SCHEMA,
        action=ErrorAction.REGENERATE,
        retryable=True,
        max_retries=2,
    ),
    ErrorCode.TOOL_TIMEOUT: ErrorPolicy(
        category=ErrorCategory.TOOL,
        action=ErrorAction.RETRY,
        retryable=True,
        max_retries=2,
    ),
    ErrorCode.TOOL_NOT_FOUND: ErrorPolicy(
        category=ErrorCategory.TOOL,
        action=ErrorAction.FAIL,
        retryable=False,
        max_retries=0,
    ),
    ErrorCode.TOOL_EXECUTION_FAILED: ErrorPolicy(
        category=ErrorCategory.TOOL,
        action=ErrorAction.RETRY,
        retryable=True,
        max_retries=1,
    ),
    ErrorCode.TOOL_OUTPUT_INVALID: ErrorPolicy(
        category=ErrorCategory.TOOL,
        action=ErrorAction.FAIL,
        retryable=False,
        max_retries=0,
    ),
    ErrorCode.PERMISSION_DENIED: ErrorPolicy(
        category=ErrorCategory.PERMISSION,
        action=ErrorAction.FAIL,
        retryable=False,
        max_retries=0,
    ),
    ErrorCode.INVALID_ARGUMENT: ErrorPolicy(
        category=ErrorCategory.SCHEMA,
        action=ErrorAction.FAIL,
        retryable=False,
        max_retries=0,
    ),
    ErrorCode.POLICY_REJECTED: ErrorPolicy(
        category=ErrorCategory.POLICY,
        action=ErrorAction.ESCALATE_POLICY,
        retryable=False,
        max_retries=0,
    ),
    ErrorCode.BUDGET_EXCEEDED: ErrorPolicy(
        category=ErrorCategory.BUDGET,
        action=ErrorAction.RETURN_PARTIAL,
        retryable=False,
        max_retries=0,
    ),
    ErrorCode.CONTEXT_TOO_LONG: ErrorPolicy(
        category=ErrorCategory.CONTEXT,
        action=ErrorAction.REDUCE_CONTEXT,
        retryable=True,
        max_retries=1,
    ),
    ErrorCode.EXTERNAL_SERVICE_ERROR: ErrorPolicy(
        category=ErrorCategory.EXTERNAL,
        action=ErrorAction.RETRY_WITH_BACKOFF,
        retryable=True,
        max_retries=2,
    ),
}

ERROR_POLICIES = MappingProxyType(_ERROR_POLICIES)


def error_policy(code: ErrorCode) -> ErrorPolicy:
    """Return the immutable default policy for an error code."""

    return ERROR_POLICIES[code]


class ErrorInfo(StrictSchema):
    """Sanitized runtime error suitable for traces and API responses."""

    code: ErrorCode
    category: ErrorCategory
    action: ErrorAction
    retryable: bool
    message: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_against_taxonomy(self) -> ErrorInfo:
        policy = error_policy(self.code)
        if self.category is not policy.category:
            raise ValueError("error category must match the taxonomy")
        if self.retryable and not policy.retryable:
            raise ValueError("non-retryable errors cannot be promoted to retryable")
        action_is_narrowed_failure = (
            policy.retryable
            and not self.retryable
            and self.action is ErrorAction.FAIL
        )
        if self.action is not policy.action and not action_is_narrowed_failure:
            raise ValueError("error action must match or safely narrow the taxonomy")
        if policy.retryable and not self.retryable and self.action is not ErrorAction.FAIL:
            raise ValueError("disabled retries must use the FAIL action")
        return self

    @classmethod
    def from_code(
        cls,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
    ) -> ErrorInfo:
        policy = error_policy(code)
        effective_retryable = policy.retryable if retryable is None else retryable
        effective_action = (
            ErrorAction.FAIL
            if policy.retryable and not effective_retryable
            else policy.action
        )
        return cls(
            code=code,
            category=policy.category,
            action=effective_action,
            retryable=effective_retryable,
            message=message,
        )
