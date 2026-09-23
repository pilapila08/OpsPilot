import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APITimeoutError, BadRequestError, RateLimitError

from opspilot.agent.schemas import ExecutionPlanV1, StrictSchema
from opspilot.llm import (
    ModelContextError,
    ModelConfigurationError,
    ModelExternalError,
    ModelMessage,
    ModelRateLimitError,
    ModelRole,
    ModelTimeoutError,
    OpenAIAdapterSettings,
    OpenAIStructuredModelClient,
    PromptReference,
    StructuredModelConfig,
    StructuredModelRequest,
    build_openai_structured_client,
)


class SampleOutput(StrictSchema):
    value: int


class FakeResponses:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _request() -> StructuredModelRequest:
    return StructuredModelRequest(
        messages=(
            ModelMessage(role=ModelRole.SYSTEM, content="prompt"),
            ModelMessage(role=ModelRole.USER, content="data"),
        ),
        prompt=PromptReference(
            component="router",
            version="v1",
            content_hash="a" * 64,
        ),
        config=StructuredModelConfig(
            provider="openai",
            model="configured-model",
            timeout_seconds=12,
            temperature=0,
            max_output_tokens=200,
            input_cost_per_million_usd=Decimal("1"),
            output_cost_per_million_usd=Decimal("2"),
        ),
    )


def test_openai_adapter_returns_shared_contract_and_never_stores_response() -> None:
    responses = FakeResponses(
        SimpleNamespace(
            output_parsed=SampleOutput(value=7),
            usage=SimpleNamespace(input_tokens=1_000, output_tokens=500),
            id="resp_001",
            model="configured-model-2026-09-22",
        )
    )
    adapter = OpenAIStructuredModelClient(responses)

    result = asyncio.run(adapter.complete(_request(), SampleOutput))

    assert result.output.value == 7
    assert result.metadata.provider == "openai"
    assert result.metadata.model_version == "configured-model-2026-09-22"
    assert result.metadata.usage.total_tokens == 1_500
    assert result.metadata.usage.cost_usd == Decimal("0.002000")
    assert responses.calls[0]["text_format"] is SampleOutput
    assert responses.calls[0]["store"] is False
    assert responses.calls[0]["timeout"] == 12
    assert responses.calls[0]["temperature"] == 0


def test_openai_adapter_validates_json_array_steps_in_plan_fallback() -> None:
    responses = FakeResponses(
        SimpleNamespace(
            output_parsed={
                "schema_version": 1,
                "steps": [
                    {
                        "step_id": 1,
                        "call_id": "call_001",
                        "tool": "k8s.get_pod_status",
                        "arguments": {"namespace": "team-a", "pod_name": "api"},
                        "reason": "Read status",
                    }
                ],
            },
            usage=SimpleNamespace(input_tokens=1, output_tokens=2),
            id="resp_002",
            model="configured-model",
        )
    )
    result = asyncio.run(
        OpenAIStructuredModelClient(responses).complete(_request(), ExecutionPlanV1)
    )
    assert result.output.steps[0].call_id == "call_001"
    assert responses.calls[0]["text_format"] is ExecutionPlanV1


def _request_and_response(status_code: int) -> tuple[httpx.Request, httpx.Response]:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return request, httpx.Response(status_code, request=request)


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (
            APITimeoutError(
                request=httpx.Request(
                    "POST",
                    "https://api.openai.com/v1/responses",
                )
            ),
            ModelTimeoutError,
        ),
        (
            RateLimitError(
                "credential=do-not-leak",
                response=_request_and_response(429)[1],
                body={"secret": "do-not-leak"},
            ),
            ModelRateLimitError,
        ),
        (
            BadRequestError(
                "secret request body",
                response=_request_and_response(400)[1],
                body={"code": "context_length_exceeded"},
            ),
            ModelContextError,
        ),
        (
            RuntimeError("api_key=do-not-leak"),
            ModelExternalError,
        ),
    ],
)
def test_openai_adapter_maps_errors_without_leaking_provider_text(
    error: Exception,
    expected_type: type[Exception],
) -> None:
    adapter = OpenAIStructuredModelClient(FakeResponses(error))

    with pytest.raises(expected_type) as caught:
        asyncio.run(adapter.complete(_request(), SampleOutput))

    assert "do-not-leak" not in str(caught.value)
    assert "api_key" not in str(caught.value)


def test_openai_factory_requires_environment_credential() -> None:
    with pytest.raises(
        ModelConfigurationError,
        match="credential is not configured",
    ):
        build_openai_structured_client(
            OpenAIAdapterSettings(),
            environ={},
        )
