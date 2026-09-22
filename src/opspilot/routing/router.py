"""Audited, budget-aware Intent Router for the single V1 diagnosis scope."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter
from typing import cast

from pydantic import JsonValue, ValidationError

from opspilot.agent.schemas import (
    BudgetState,
    IntentOutput,
    Target,
)
from opspilot.llm.audit import (
    ModelAuditError,
    ModelAuditRepository,
    ModelCallAttempt,
)
from opspilot.llm.client import StructuredModelClient
from opspilot.llm.errors import (
    ModelBudgetError,
    ModelExternalError,
    ModelGatewayError,
    ModelSchemaError,
    RouterScopeError,
)
from opspilot.llm.models import (
    ModelMessage,
    ModelRole,
    ModelUsage,
    PromptReference,
    PromptTemplate,
    StructuredModelConfig,
    StructuredModelRequest,
)
from opspilot.routing.models import (
    RouterInput,
    RouterModelOutput,
    RouterOutcome,
    RouterSettings,
)

_REGENERATION_MESSAGE = (
    "The previous response failed schema validation. Return a new object that "
    "matches the requested schema exactly. Do not repeat user input."
)


class IntentRouter:
    """Map untrusted user text to the one supported V1 intent."""

    def __init__(
        self,
        *,
        client: StructuredModelClient,
        audit_repository: ModelAuditRepository,
        prompt: PromptTemplate,
        model_config: StructuredModelConfig,
        settings: RouterSettings | None = None,
    ) -> None:
        if prompt.component != "router":
            raise ValueError("IntentRouter requires a router prompt")
        self._client = client
        self._audit = audit_repository
        self._prompt = prompt
        self._model_config = model_config
        self._settings = settings or RouterSettings()

    async def route(
        self,
        *,
        run_id: str,
        query: str,
        namespace: str,
        budget: BudgetState,
    ) -> RouterOutcome:
        try:
            router_input = RouterInput(
                run_id=run_id,
                query=query,
                namespace=namespace,
            )
        except ValidationError:
            raise RouterScopeError("router input is invalid") from None
        return await self.route_input(router_input, budget=budget)

    async def route_input(
        self,
        router_input: RouterInput,
        *,
        budget: BudgetState,
    ) -> RouterOutcome:
        _ensure_model_budget(budget)
        try:
            prompt_version_id = self._audit.register_prompt(self._prompt)
        except ModelAuditError:
            raise ModelExternalError(
                "model audit prompt could not be registered"
            ) from None

        messages = _initial_messages(self._prompt, router_input)
        call_ids: list[str] = []
        current_budget = budget

        for attempt_index in range(self._settings.max_schema_retries + 1):
            _ensure_model_budget(current_budget)
            request = StructuredModelRequest(
                messages=tuple(messages),
                prompt=PromptReference.from_template(self._prompt),
                config=self._model_config,
            )
            started_at = datetime.now(UTC)
            started_clock = perf_counter()
            try:
                result = await self._client.complete(
                    request,
                    RouterModelOutput,
                )
            except ModelGatewayError as exc:
                completed_at = datetime.now(UTC)
                elapsed_ms = max(
                    0,
                    round((perf_counter() - started_clock) * 1_000),
                )
                usage = exc.usage or ModelUsage()
                latency_ms = (
                    exc.latency_ms
                    if exc.latency_ms is not None
                    else elapsed_ms
                )
                recorded = self._append_attempt(
                    router_input=router_input,
                    prompt_version_id=prompt_version_id,
                    attempt_index=attempt_index,
                    started_at=started_at,
                    completed_at=completed_at,
                    usage=usage,
                    latency_ms=latency_ms,
                    model_version=exc.model_version,
                    output=None,
                    error=exc,
                )
                call_ids.append(recorded)
                current_budget = _consume_usage(
                    current_budget,
                    usage=usage,
                    latency_ms=latency_ms,
                )
                if not isinstance(exc, ModelSchemaError):
                    raise
                if attempt_index >= self._settings.max_schema_retries:
                    raise
                if (
                    current_budget.retries_used
                    >= current_budget.limits.max_retries
                ):
                    raise ModelBudgetError(
                        "schema regeneration retry budget is exhausted"
                    ) from None
                current_budget = _consume_retry(current_budget)
                messages.append(
                    ModelMessage(
                        role=ModelRole.DEVELOPER,
                        content=_REGENERATION_MESSAGE,
                    )
                )
                continue

            completed_at = datetime.now(UTC)
            recorded = self._append_attempt(
                router_input=router_input,
                prompt_version_id=prompt_version_id,
                attempt_index=attempt_index,
                started_at=started_at,
                completed_at=completed_at,
                usage=result.metadata.usage,
                latency_ms=result.metadata.latency_ms,
                model_version=result.metadata.model_version,
                output=result.output,
                error=None,
            )
            call_ids.append(recorded)
            current_budget = _consume_usage(
                current_budget,
                usage=result.metadata.usage,
                latency_ms=result.metadata.latency_ms,
            )
            intent = _apply_scope(result.output, router_input)
            return RouterOutcome(
                intent=intent,
                budget=current_budget,
                attempts=attempt_index + 1,
                llm_call_ids=tuple(call_ids),
            )

        raise ModelSchemaError("model output failed schema validation")

    def _append_attempt(
        self,
        *,
        router_input: RouterInput,
        prompt_version_id: str,
        attempt_index: int,
        started_at: datetime,
        completed_at: datetime,
        usage: ModelUsage,
        latency_ms: int,
        model_version: str | None,
        output: RouterModelOutput | None,
        error: ModelGatewayError | None,
    ) -> str:
        request_payload: dict[str, JsonValue] = {
            "namespace": router_input.namespace,
            "query_sha256": sha256(
                router_input.query.encode("utf-8")
            ).hexdigest(),
            "query_length": len(router_input.query),
            "output_schema": RouterModelOutput.__name__,
            "attempt": attempt_index + 1,
        }
        response_payload = (
            cast(
                dict[str, JsonValue],
                output.model_dump(mode="json"),
            )
            if output is not None
            else None
        )
        try:
            recorded = self._audit.append_attempt(
                ModelCallAttempt(
                    run_id=router_input.run_id,
                    component="router",
                    prompt_version_id=prompt_version_id,
                    provider=self._model_config.provider,
                    model_name=self._model_config.model,
                    model_version=(
                        model_version or self._model_config.model_version
                    ),
                    request_payload=request_payload,
                    response_payload=response_payload,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd,
                    latency_ms=latency_ms,
                    retry_count=attempt_index,
                    success=error is None,
                    error_code=error.code if error is not None else None,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        except ModelAuditError:
            raise ModelExternalError(
                "model call audit could not be persisted"
            ) from None
        return recorded.call_id


def _initial_messages(
    prompt: PromptTemplate,
    router_input: RouterInput,
) -> list[ModelMessage]:
    user_data = json.dumps(
        {
            "namespace": router_input.namespace,
            "query": router_input.query,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return [
        ModelMessage(role=ModelRole.SYSTEM, content=prompt.content),
        ModelMessage(role=ModelRole.USER, content=user_data),
    ]


def _apply_scope(
    output: RouterModelOutput,
    router_input: RouterInput,
) -> IntentOutput:
    if (
        output.intent != "diagnose"
        or output.domain != "kubernetes"
        or output.problem_type != "pod_restart"
    ):
        raise RouterScopeError(
            "router output is outside the supported V1 diagnosis scope"
        )
    return IntentOutput(
        intent="diagnose",
        domain="kubernetes",
        problem_type="pod_restart",
        target=Target(
            namespace=router_input.namespace,
            resource=output.resource,
        ),
    )


def _ensure_model_budget(budget: BudgetState) -> None:
    exhausted = set(budget.exhausted_dimensions)
    if exhausted & {"tokens", "cost", "time"}:
        raise ModelBudgetError("model call budget is exhausted")


def _consume_usage(
    budget: BudgetState,
    *,
    usage: ModelUsage,
    latency_ms: int,
) -> BudgetState:
    return BudgetState(
        limits=budget.limits,
        steps_used=budget.steps_used,
        tool_calls_used=budget.tool_calls_used,
        retries_used=budget.retries_used,
        tokens_used=budget.tokens_used + usage.total_tokens,
        cost_usd=budget.cost_usd + usage.cost_usd,
        elapsed_seconds=budget.elapsed_seconds + latency_ms / 1_000,
    )


def _consume_retry(budget: BudgetState) -> BudgetState:
    return BudgetState(
        limits=budget.limits,
        steps_used=budget.steps_used,
        tool_calls_used=budget.tool_calls_used,
        retries_used=budget.retries_used + 1,
        tokens_used=budget.tokens_used,
        cost_usd=budget.cost_usd,
        elapsed_seconds=budget.elapsed_seconds,
    )
