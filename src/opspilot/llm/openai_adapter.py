"""OpenAI Responses adapter for provider-neutral structured generation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from time import perf_counter
from typing import Any, Protocol, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, Field, ValidationError

from opspilot.agent.schemas import ExecutionPlanV1
from opspilot.diagnosis.models import DiagnosisDraftV1
from opspilot.llm.strict_wire import StrictJsonEnvelope
from opspilot.agent.schemas import StrictSchema
from opspilot.llm.errors import (
    ModelConfigurationError,
    ModelContextError,
    ModelExternalError,
    ModelGatewayError,
    ModelPermissionError,
    ModelRateLimitError,
    ModelSchemaError,
    ModelTimeoutError,
)
from opspilot.llm.models import (
    ModelResponseMetadata,
    ModelUsage,
    OutputT,
    StructuredModelRequest,
    StructuredModelResult,
)

_MILLION = Decimal("1000000")
_COST_QUANTUM = Decimal("0.000001")


class OpenAIResponsesAPI(Protocol):
    async def parse(self, **kwargs: Any) -> Any: ...


class OpenAIAdapterSettings(StrictSchema):
    """Connection settings; secrets are resolved only from environment."""

    api_key_env: str = Field(
        default="OPENAI_API_KEY",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    organization_env: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    project_env: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )


class OpenAIStructuredModelClient:
    """Translate OpenAI Responses into the shared structured result."""

    def __init__(
        self,
        responses_api: OpenAIResponsesAPI,
        *,
        close_callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._responses_api = responses_api
        self._close_callback = close_callback

    async def complete(
        self,
        request: StructuredModelRequest,
        output_model: type[OutputT],
    ) -> StructuredModelResult[OutputT]:
        if request.config.provider != "openai":
            raise ModelConfigurationError(
                "OpenAI adapter requires provider=openai"
            )

        input_messages = [
            {
                "role": message.role.value,
                "content": message.content,
            }
            for message in request.messages
        ]
        wire_model = cast(
            type[BaseModel],
            StrictJsonEnvelope
            if output_model in {ExecutionPlanV1, DiagnosisDraftV1}
            else output_model,
        )
        call_arguments: dict[str, Any] = {
            "model": request.config.model,
            "input": input_messages,
            "text_format": wire_model,
            "max_output_tokens": request.config.max_output_tokens,
            "store": False,
            "timeout": request.config.timeout_seconds,
        }
        if request.config.temperature is not None:
            call_arguments["temperature"] = request.config.temperature

        started = perf_counter()
        try:
            response = await self._responses_api.parse(**call_arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _translate_openai_error(exc) from None
        latency_ms = max(0, round((perf_counter() - started) * 1_000))

        input_tokens, output_tokens = _usage_tokens(
            getattr(response, "usage", None)
        )
        cost = _calculate_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            request=request,
        )
        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost,
        )
        actual_model = getattr(response, "model", request.config.model)
        if not isinstance(actual_model, str):
            actual_model = request.config.model
        model_version = request.config.model_version
        if model_version is None and actual_model != request.config.model:
            model_version = actual_model

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise ModelSchemaError(
                "model response did not contain structured output",
                usage=usage,
                latency_ms=latency_ms,
                model_version=model_version,
            )
        try:
            wire_output = (
                parsed
                if isinstance(parsed, wire_model)
                else wire_model.model_validate_json(json.dumps(parsed), strict=True)
            )
            output = (
                output_model.model_validate_json(
                    wire_output.payload_json, strict=True,
                )
                if isinstance(wire_output, StrictJsonEnvelope)
                else wire_output
            )
        except (TypeError, ValueError, ValidationError):
            raise ModelSchemaError(
                "model output failed schema validation",
                usage=usage,
                latency_ms=latency_ms,
                model_version=model_version,
            ) from None

        response_id = getattr(response, "id", None)
        if not isinstance(response_id, str):
            response_id = None

        return StructuredModelResult[OutputT](
            output=cast(OutputT, output),
            metadata=ModelResponseMetadata(
                provider="openai",
                model=request.config.model,
                model_version=model_version,
                response_id=response_id,
                latency_ms=latency_ms,
                usage=usage,
            ),
        )

    async def close(self) -> None:
        if self._close_callback is not None:
            await self._close_callback()


def build_openai_structured_client(
    settings: OpenAIAdapterSettings,
    *,
    environ: Mapping[str, str] | None = None,
) -> OpenAIStructuredModelClient:
    environment = os.environ if environ is None else environ
    api_key = environment.get(settings.api_key_env)
    if not api_key:
        raise ModelConfigurationError(
            "OpenAI API credential is not configured"
        )
    organization = (
        environment.get(settings.organization_env)
        if settings.organization_env is not None
        else None
    )
    project = (
        environment.get(settings.project_env)
        if settings.project_env is not None
        else None
    )
    client = AsyncOpenAI(
        api_key=api_key,
        organization=organization,
        project=project,
        max_retries=0,
    )
    return OpenAIStructuredModelClient(
        cast(OpenAIResponsesAPI, client.responses),
        close_callback=client.close,
    )


def _usage_tokens(usage: object) -> tuple[int, int]:
    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        raise ModelExternalError("model usage metadata is invalid")
    return input_tokens, output_tokens


def _calculate_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    request: StructuredModelRequest,
) -> Decimal:
    cost = (
        Decimal(input_tokens)
        * request.config.input_cost_per_million_usd
        / _MILLION
        + Decimal(output_tokens)
        * request.config.output_cost_per_million_usd
        / _MILLION
    )
    return cost.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)


def _translate_openai_error(exc: Exception) -> ModelGatewayError:
    if isinstance(exc, RateLimitError):
        return ModelRateLimitError("model provider rate limit exceeded")
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return ModelTimeoutError("model provider request timed out")
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return ModelPermissionError("model provider access was denied")
    if isinstance(exc, BadRequestError):
        if getattr(exc, "code", None) == "context_length_exceeded":
            return ModelContextError("model context limit was exceeded")
        return ModelExternalError("model provider rejected the request")
    if isinstance(exc, (APIConnectionError, APIStatusError)):
        return ModelExternalError("model provider request failed")
    return ModelExternalError("model provider call failed")
