import asyncio
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from openai import APITimeoutError, BadRequestError, RateLimitError
from openai.lib._pydantic import to_strict_json_schema

from opspilot.agent.schemas import ExecutionPlanV1, StrictSchema
from opspilot.diagnosis.models import DiagnosisDraftV1
from opspilot.llm import (
    ModelContextError,
    ModelConfigurationError,
    ModelExternalError,
    ModelMessage,
    ModelRateLimitError,
    ModelSchemaError,
    ModelRole,
    ModelTimeoutError,
    OpenAIAdapterSettings,
    OpenAIStructuredModelClient,
    PromptReference,
    StructuredModelConfig,
    StructuredModelRequest,
    build_openai_structured_client,
)
from opspilot.llm.strict_wire import StrictJsonEnvelope


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
    plan = {
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
    }
    responses = FakeResponses(
        SimpleNamespace(
            output_parsed={"payload_json": json.dumps(plan)},
            usage=SimpleNamespace(input_tokens=1, output_tokens=2),
            id="resp_002",
            model="configured-model",
        )
    )
    result = asyncio.run(
        OpenAIStructuredModelClient(responses).complete(_request(), ExecutionPlanV1)
    )
    assert result.output.steps[0].call_id == "call_001"
    assert result.output.steps[0].arguments == {
        "namespace": "team-a", "pod_name": "api",
    }
    assert responses.calls[0]["text_format"] is StrictJsonEnvelope


def test_domain_wire_schema_is_strict_and_has_no_dynamic_object() -> None:
    schema = to_strict_json_schema(StrictJsonEnvelope)
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    properties = cast(dict[str, Any], schema["properties"])
    assert isinstance(properties, dict)
    assert schema["required"] == ["payload_json"]
    payload = properties["payload_json"]
    assert isinstance(payload, dict)
    assert payload["type"] == "string"
    assert "$defs" not in schema


def test_invalid_plan_json_is_a_schema_error_with_usage() -> None:
    responses = FakeResponses(
        SimpleNamespace(
            output_parsed={"payload_json": '{"schema_version":1,"steps":[]}'},
            usage=SimpleNamespace(input_tokens=5, output_tokens=7),
            id="resp_invalid",
            model="configured-model",
        )
    )
    with pytest.raises(ModelSchemaError) as caught:
        asyncio.run(OpenAIStructuredModelClient(responses).complete(_request(), ExecutionPlanV1))
    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 12


def test_diagnosis_wire_decodes_to_domain_schema() -> None:
    draft = {
        "schema_version": 1,
        "root_cause": "Early liveness probe",
        "recommendation": "Add a startup probe",
        "claims": [{
            "claim_id": "claim_001",
            "text": "Liveness interrupts startup",
            "evidence_ids": ["ev_001"],
            "inference_confidence": 0.5,
        }],
    }
    responses = FakeResponses(
        SimpleNamespace(
            output_parsed={"payload_json": json.dumps(draft)},
            usage=SimpleNamespace(input_tokens=5, output_tokens=7),
            id="resp_diagnosis",
            model="configured-model",
        )
    )
    result = asyncio.run(
        OpenAIStructuredModelClient(responses).complete(_request(), DiagnosisDraftV1)
    )
    assert result.output.claims[0].evidence_ids == ("ev_001",)
    assert responses.calls[0]["text_format"] is StrictJsonEnvelope


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
