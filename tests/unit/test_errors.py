import pytest
from pydantic import ValidationError

from opspilot.errors import (
    ERROR_POLICIES,
    ErrorAction,
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    ErrorPolicy,
    error_policy,
)


@pytest.mark.parametrize(
    ("code", "category", "action", "retryable", "max_retries"),
    [
        (
            ErrorCode.LLM_RATE_LIMIT,
            ErrorCategory.LLM,
            ErrorAction.RETRY_WITH_BACKOFF,
            True,
            3,
        ),
        (
            ErrorCode.LLM_TIMEOUT,
            ErrorCategory.LLM,
            ErrorAction.RETRY,
            True,
            2,
        ),
        (
            ErrorCode.SCHEMA_VALIDATION,
            ErrorCategory.SCHEMA,
            ErrorAction.REGENERATE,
            True,
            2,
        ),
        (
            ErrorCode.TOOL_TIMEOUT,
            ErrorCategory.TOOL,
            ErrorAction.RETRY,
            True,
            2,
        ),
        (
            ErrorCode.TOOL_NOT_FOUND,
            ErrorCategory.TOOL,
            ErrorAction.FAIL,
            False,
            0,
        ),
        (
            ErrorCode.TOOL_EXECUTION_FAILED,
            ErrorCategory.TOOL,
            ErrorAction.RETRY,
            True,
            1,
        ),
        (
            ErrorCode.TOOL_OUTPUT_INVALID,
            ErrorCategory.TOOL,
            ErrorAction.FAIL,
            False,
            0,
        ),
        (
            ErrorCode.PERMISSION_DENIED,
            ErrorCategory.PERMISSION,
            ErrorAction.FAIL,
            False,
            0,
        ),
        (
            ErrorCode.INVALID_ARGUMENT,
            ErrorCategory.SCHEMA,
            ErrorAction.FAIL,
            False,
            0,
        ),
        (
            ErrorCode.POLICY_REJECTED,
            ErrorCategory.POLICY,
            ErrorAction.ESCALATE_POLICY,
            False,
            0,
        ),
        (
            ErrorCode.BUDGET_EXCEEDED,
            ErrorCategory.BUDGET,
            ErrorAction.RETURN_PARTIAL,
            False,
            0,
        ),
        (
            ErrorCode.CONTEXT_TOO_LONG,
            ErrorCategory.CONTEXT,
            ErrorAction.REDUCE_CONTEXT,
            True,
            1,
        ),
        (
            ErrorCode.EXTERNAL_SERVICE_ERROR,
            ErrorCategory.EXTERNAL,
            ErrorAction.RETRY_WITH_BACKOFF,
            True,
            2,
        ),
    ],
)
def test_error_policy_matrix(
    code: ErrorCode,
    category: ErrorCategory,
    action: ErrorAction,
    retryable: bool,
    max_retries: int,
) -> None:
    policy = error_policy(code)

    assert policy.category is category
    assert policy.action is action
    assert policy.retryable is retryable
    assert policy.max_retries == max_retries


def test_every_error_code_has_exactly_one_policy() -> None:
    assert set(ERROR_POLICIES) == set(ErrorCode)


@pytest.mark.parametrize(
    ("retryable", "max_retries"),
    [(False, 1), (True, 0)],
)
def test_error_policy_rejects_inconsistent_retry_configuration(
    retryable: bool,
    max_retries: int,
) -> None:
    with pytest.raises(ValidationError):
        ErrorPolicy(
            category=ErrorCategory.TOOL,
            action=ErrorAction.RETRY,
            retryable=retryable,
            max_retries=max_retries,
        )


def test_error_info_uses_canonical_taxonomy_and_round_trips() -> None:
    error = ErrorInfo.from_code(
        ErrorCode.TOOL_TIMEOUT,
        "tool execution timed out",
    )

    restored = ErrorInfo.model_validate_json(error.model_dump_json())

    assert restored == error
    assert error.category is ErrorCategory.TOOL
    assert error.action is ErrorAction.RETRY
    assert error.retryable is True


def test_retryable_error_can_be_narrowed_for_a_specific_call() -> None:
    error = ErrorInfo.from_code(
        ErrorCode.TOOL_TIMEOUT,
        "tool execution timed out",
        retryable=False,
    )

    assert error.retryable is False
    assert error.action is ErrorAction.FAIL


def test_non_retryable_error_cannot_be_promoted() -> None:
    with pytest.raises(ValidationError):
        ErrorInfo.from_code(
            ErrorCode.PERMISSION_DENIED,
            "permission denied",
            retryable=True,
        )


def test_error_info_rejects_category_or_action_drift() -> None:
    with pytest.raises(ValidationError):
        ErrorInfo(
            code=ErrorCode.BUDGET_EXCEEDED,
            category=ErrorCategory.TOOL,
            action=ErrorAction.FAIL,
            retryable=False,
            message="budget exceeded",
        )
