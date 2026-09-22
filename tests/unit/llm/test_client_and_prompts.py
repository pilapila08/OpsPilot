import asyncio
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from opspilot.agent.schemas import StrictSchema
from opspilot.llm import (
    ModelMessage,
    ModelRole,
    ModelSchemaError,
    PromptReference,
    PromptTemplate,
    ScriptedModelClient,
    ScriptedModelResponse,
    StructuredModelConfig,
    StructuredModelRequest,
    load_prompt,
)


class SampleOutput(StrictSchema):
    value: int


def _request(prompt: PromptTemplate) -> StructuredModelRequest:
    return StructuredModelRequest(
        messages=(
            ModelMessage(role=ModelRole.SYSTEM, content=prompt.content),
            ModelMessage(role=ModelRole.USER, content="untrusted data"),
        ),
        prompt=PromptReference.from_template(prompt),
        config=StructuredModelConfig(
            provider="scripted",
            model="test-model",
        ),
    )


def test_prompt_loader_hashes_exact_versioned_content(tmp_path: Path) -> None:
    path = tmp_path / "prompt.md"
    path.write_text("System prompt\n", encoding="utf-8")

    prompt = load_prompt(path, component="router", version="v1")

    assert prompt.content == "System prompt\n"
    assert prompt.content_hash == sha256(b"System prompt\n").hexdigest()


def test_prompt_contract_rejects_mismatched_hash() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        PromptTemplate(
            component="router",
            version="v1",
            content="prompt",
            content_hash="0" * 64,
        )


def test_scripted_client_validates_schema_and_records_request() -> None:
    prompt = PromptTemplate(
        component="router",
        version="v1",
        content="prompt",
        content_hash=sha256(b"prompt").hexdigest(),
    )
    client = ScriptedModelClient(
        [
            ScriptedModelResponse(
                payload={"value": 7},
                input_tokens=10,
                output_tokens=2,
                cost_usd=Decimal("0.000012"),
                latency_ms=3,
            )
        ]
    )

    result = asyncio.run(client.complete(_request(prompt), SampleOutput))

    assert result.output.value == 7
    assert result.metadata.usage.total_tokens == 12
    assert result.metadata.usage.cost_usd == Decimal("0.000012")
    assert client.call_count == 1
    assert client.output_models == (SampleOutput,)
    assert client.requests[0].prompt.version == "v1"
    client.assert_exhausted()


def test_scripted_client_schema_failure_keeps_usage_without_raw_payload() -> None:
    prompt = PromptTemplate(
        component="router",
        version="v1",
        content="prompt",
        content_hash=sha256(b"prompt").hexdigest(),
    )
    client = ScriptedModelClient(
        [
            ScriptedModelResponse(
                payload={"value": "not-an-int", "secret": "do-not-leak"},
                input_tokens=9,
                output_tokens=4,
                latency_ms=5,
            )
        ]
    )

    with pytest.raises(ModelSchemaError) as caught:
        asyncio.run(client.complete(_request(prompt), SampleOutput))

    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 13
    assert caught.value.latency_ms == 5
    assert "secret" not in caught.value.safe_message
