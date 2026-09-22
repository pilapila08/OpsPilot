from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.tools.models import (
    InvalidToolDefinition,
    RetryPolicy,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
    ToolMetadata,
    ToolResponse,
    ToolRiskLevel,
)


class EchoInput(StrictSchema):
    value: int


class EchoOutput(StrictSchema):
    echoed: int


async def echo_handler(arguments: EchoInput) -> EchoOutput:
    return EchoOutput(echoed=arguments.value)


def make_definition() -> ToolDefinition[EchoInput, EchoOutput]:
    return ToolDefinition(
        name="test.echo",
        description="Echo a validated integer",
        risk_level=ToolRiskLevel.READ_ONLY,
        input_model=EchoInput,
        output_model=EchoOutput,
        handler=echo_handler,
        source="test",
    )


def make_metadata() -> ToolMetadata:
    return ToolMetadata(
        call_id="call_001",
        tool_name="test.echo",
        source="test",
        duration_ms=1,
        tool_version="v1",
    )


def test_definition_publishes_closed_input_and_output_schemas() -> None:
    descriptor = make_definition().descriptor

    assert descriptor.input_schema["additionalProperties"] is False
    assert descriptor.output_schema["additionalProperties"] is False
    assert descriptor.risk_level is ToolRiskLevel.READ_ONLY


def test_definition_rejects_invalid_name() -> None:
    with pytest.raises(InvalidToolDefinition):
        ToolDefinition(
            name="execute_shell",
            description="Unsafe unscoped command execution",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=echo_handler,
            source="test",
        )


def test_definition_rejects_invalid_timeout() -> None:
    with pytest.raises(InvalidToolDefinition):
        ToolDefinition(
            name="test.echo",
            description="Echo a validated integer",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=echo_handler,
            source="test",
            timeout_seconds=0,
        )


def test_definition_rejects_invalid_risk_level() -> None:
    with pytest.raises(InvalidToolDefinition):
        ToolDefinition(
            name="test.echo",
            description="Echo a validated integer",
            risk_level=cast(ToolRiskLevel, 9),
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=echo_handler,
            source="test",
        )


def test_definition_requires_async_handler() -> None:
    def sync_handler(arguments: EchoInput) -> EchoOutput:
        return EchoOutput(echoed=arguments.value)

    with pytest.raises(InvalidToolDefinition):
        ToolDefinition(
            name="test.echo",
            description="Echo a validated integer",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=EchoInput,
            output_model=EchoOutput,
            handler=cast(ToolHandler[EchoInput, EchoOutput], sync_handler),
            source="test",
        )


def test_invocation_accepts_only_json_arguments() -> None:
    invalid_arguments = cast(dict[str, JsonValue], {"value": object()})

    with pytest.raises(ValidationError):
        ToolInvocation(
            call_id="call_001",
            tool="test.echo",
            arguments=invalid_arguments,
        )


@pytest.mark.parametrize(
    ("success", "data", "error"),
    [
        (
            True,
            None,
            None,
        ),
        (
            True,
            {"echoed": 1},
            ErrorInfo.from_code(
                ErrorCode.TOOL_EXECUTION_FAILED,
                "failed",
            ),
        ),
        (False, {"echoed": 1}, None),
        (False, None, None),
    ],
)
def test_response_enforces_success_and_failure_shape(
    success: bool,
    data: dict[str, JsonValue] | None,
    error: ErrorInfo | None,
) -> None:
    with pytest.raises(ValidationError):
        ToolResponse(
            success=success,
            data=data,
            metadata=make_metadata(),
            error=error,
        )


def test_retry_policy_rejects_invalid_or_duplicate_configuration() -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(initial_backoff_seconds=2, max_backoff_seconds=1)

    with pytest.raises(ValidationError):
        RetryPolicy(
            retryable_errors=(
                ErrorCode.TOOL_TIMEOUT,
                ErrorCode.TOOL_TIMEOUT,
            )
        )

    with pytest.raises(ValidationError):
        RetryPolicy(retryable_errors=(ErrorCode.INVALID_ARGUMENT,))
