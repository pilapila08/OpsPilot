import asyncio
from typing import cast

import pytest
from pydantic import JsonValue

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorAction, ErrorCategory, ErrorCode
from opspilot.tools.models import (
    RetryPolicy,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
    ToolResponse,
    ToolRiskLevel,
)
from opspilot.tools.registry import (
    DuplicateToolError,
    ToolNotRegisteredError,
    ToolRegistry,
)


class EchoInput(StrictSchema):
    value: int


class EchoOutput(StrictSchema):
    echoed: int


async def echo_handler(arguments: EchoInput) -> EchoOutput:
    return EchoOutput(echoed=arguments.value)


def make_definition(
    *,
    name: str = "test.echo",
    risk_level: ToolRiskLevel = ToolRiskLevel.READ_ONLY,
    timeout_seconds: float = 1,
    retry_policy: RetryPolicy | None = None,
    handler: ToolHandler[EchoInput, EchoOutput] = echo_handler,
) -> ToolDefinition[EchoInput, EchoOutput]:
    return ToolDefinition(
        name=name,
        description="Echo a validated integer",
        risk_level=risk_level,
        input_model=EchoInput,
        output_model=EchoOutput,
        handler=handler,
        source="test",
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy or RetryPolicy(),
    )


def invoke(
    registry: ToolRegistry,
    *,
    tool: str = "test.echo",
    arguments: dict[str, JsonValue] | None = None,
) -> ToolResponse:
    invocation = ToolInvocation(
        call_id="call_001",
        tool=tool,
        arguments=arguments or {"value": 7},
    )
    return asyncio.run(registry.invoke(invocation))


def test_registry_executes_validated_read_only_tool() -> None:
    registry = ToolRegistry()
    registry.register(make_definition())

    response = invoke(registry)

    assert response.success is True
    assert response.data == {"echoed": 7}
    assert response.error is None
    assert response.metadata.source == "test"
    assert response.metadata.tool_version == "v1"


def test_registry_rejects_duplicate_registration() -> None:
    registry = ToolRegistry()
    registry.register(make_definition())

    with pytest.raises(DuplicateToolError):
        registry.register(make_definition())


def test_registry_rejects_direct_lookup_of_unknown_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolNotRegisteredError, match="not registered"):
        registry.get("test.missing")


def test_registry_returns_sorted_immutable_descriptors() -> None:
    registry = ToolRegistry()
    registry.register(make_definition(name="test.second"))
    registry.register(make_definition(name="test.first"))

    descriptors = registry.descriptors()

    assert isinstance(descriptors, tuple)
    assert [descriptor.name for descriptor in descriptors] == [
        "test.first",
        "test.second",
    ]


def test_unregistered_invocation_returns_normalized_error() -> None:
    response = invoke(ToolRegistry(), tool="test.missing")

    assert response.success is False
    assert response.data is None
    assert response.error is not None
    assert response.error.code is ErrorCode.TOOL_NOT_FOUND
    assert response.error.retryable is False


def test_invalid_arguments_do_not_reach_handler() -> None:
    called = False

    async def tracked_handler(arguments: EchoInput) -> EchoOutput:
        nonlocal called
        called = True
        return EchoOutput(echoed=arguments.value)

    registry = ToolRegistry()
    registry.register(make_definition(handler=tracked_handler))

    response = invoke(registry, arguments={"value": "not-an-integer"})

    assert called is False
    assert response.error is not None
    assert response.error.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "risk_level",
    [ToolRiskLevel.APPROVAL_REQUIRED, ToolRiskLevel.PROHIBITED],
)
def test_non_read_only_tool_requires_policy_authorization(
    risk_level: ToolRiskLevel,
) -> None:
    registry = ToolRegistry()
    registry.register(make_definition(risk_level=risk_level))

    response = invoke(registry)

    assert response.error is not None
    assert response.error.code is ErrorCode.POLICY_REJECTED


def test_timeout_is_normalized_and_marked_retryable_by_policy() -> None:
    async def slow_handler(arguments: EchoInput) -> EchoOutput:
        await asyncio.sleep(0.05)
        return EchoOutput(echoed=arguments.value)

    registry = ToolRegistry()
    registry.register(
        make_definition(
            timeout_seconds=0.01,
            retry_policy=RetryPolicy(max_retries=1),
            handler=slow_handler,
        )
    )

    response = invoke(registry)

    assert response.error is not None
    assert response.error.code is ErrorCode.TOOL_TIMEOUT
    assert response.error.category is ErrorCategory.TOOL
    assert response.error.action is ErrorAction.RETRY
    assert response.error.retryable is True


def test_invalid_output_is_normalized() -> None:
    async def invalid_handler(arguments: EchoInput) -> EchoOutput:
        return cast(EchoOutput, {"unexpected": arguments.value})

    registry = ToolRegistry()
    registry.register(make_definition(handler=invalid_handler))

    response = invoke(registry)

    assert response.error is not None
    assert response.error.code is ErrorCode.TOOL_OUTPUT_INVALID


def test_internal_exception_is_not_exposed() -> None:
    async def failing_handler(arguments: EchoInput) -> EchoOutput:
        raise RuntimeError(f"database_password=secret-{arguments.value}")

    registry = ToolRegistry()
    registry.register(make_definition(handler=failing_handler))

    response = invoke(registry)

    assert response.error is not None
    assert response.error.code is ErrorCode.TOOL_EXECUTION_FAILED
    assert response.error.action is ErrorAction.FAIL
    assert response.error.retryable is False
    assert "secret" not in response.error.message
    assert "database_password" not in response.model_dump_json()
