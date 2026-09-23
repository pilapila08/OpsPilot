"""Structured model Protocol and deterministic scripted implementation."""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from opspilot.llm.errors import (
    ModelExternalError,
    ModelGatewayError,
    ModelSchemaError,
)
from opspilot.llm.models import (
    ModelResponseMetadata,
    ModelUsage,
    OutputT,
    ScriptedModelResponse,
    StructuredModelRequest,
    StructuredModelResult,
)


@runtime_checkable
class StructuredModelClient(Protocol):
    """Provider-neutral structured generation boundary."""

    async def complete(
        self,
        request: StructuredModelRequest,
        output_model: type[OutputT],
    ) -> StructuredModelResult[OutputT]: ...


ScriptedStep = ScriptedModelResponse | ModelGatewayError


class ScriptedModelClient:
    """Return validated scripted results or errors in call order."""

    def __init__(self, steps: Sequence[ScriptedStep]) -> None:
        self._steps = tuple(steps)
        self._index = 0
        self._requests: list[StructuredModelRequest] = []
        self._output_models: list[type[BaseModel]] = []

    @property
    def call_count(self) -> int:
        return self._index

    @property
    def requests(self) -> tuple[StructuredModelRequest, ...]:
        return tuple(self._requests)

    @property
    def output_models(self) -> tuple[type[BaseModel], ...]:
        return tuple(self._output_models)

    def assert_exhausted(self) -> None:
        if self._index != len(self._steps):
            raise AssertionError(
                f"{len(self._steps) - self._index} scripted model steps remain"
            )

    async def complete(
        self,
        request: StructuredModelRequest,
        output_model: type[OutputT],
    ) -> StructuredModelResult[OutputT]:
        self._requests.append(request)
        self._output_models.append(output_model)
        if self._index >= len(self._steps):
            raise ModelExternalError("scripted model response is missing")

        step = self._steps[self._index]
        self._index += 1
        if isinstance(step, ModelGatewayError):
            raise step

        try:
            output = output_model.model_validate_json(
                json.dumps(step.payload), strict=True
            )
        except ValidationError:
            usage = ModelUsage(
                input_tokens=step.input_tokens,
                output_tokens=step.output_tokens,
                total_tokens=step.input_tokens + step.output_tokens,
                cost_usd=step.cost_usd,
            )
            raise ModelSchemaError(
                "model output failed schema validation",
                usage=usage,
                latency_ms=step.latency_ms,
                model_version=(
                    step.model_version or request.config.model_version
                ),
            ) from None

        usage = ModelUsage(
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            total_tokens=step.input_tokens + step.output_tokens,
            cost_usd=step.cost_usd,
        )
        return StructuredModelResult[OutputT](
            output=output,
            metadata=ModelResponseMetadata(
                provider=request.config.provider,
                model=request.config.model,
                model_version=(
                    step.model_version or request.config.model_version
                ),
                response_id=step.response_id,
                latency_ms=step.latency_ms,
                usage=usage,
            ),
        )
