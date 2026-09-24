"""Audited V2 fault hypothesis routing without modifying the V1 Router."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter
from typing import Literal

from pydantic import Field, JsonValue, ValidationError, model_validator

from opspilot.agent.schemas import BudgetState, StrictSchema
from opspilot.faults import (
    FaultCandidateV2,
    FaultFamilyV2,
    TargetKindCandidateV2,
    TargetKindV2,
)
from opspilot.integrations.kubernetes.models import NamespaceName, ResourceName
from opspilot.llm.audit import ModelAuditError, ModelAuditRepository, ModelCallAttempt
from opspilot.llm.budget import consume_retry, consume_usage, ensure_model_budget
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
from opspilot.routing.models import RouterInput, RouterSettings

_REGENERATION_MESSAGE = (
    "The prior response did not match the requested schema. Return only the "
    "specified fields and enum values; do not repeat user input."
)
_POD_FAMILIES = frozenset(
    {
        "crashloop_backoff",
        "liveness_probe_failed",
        "readiness_probe_failed",
        "oom_killed",
        "image_pull_backoff",
    }
)


class V2RouterModelOutput(StrictSchema):
    """Flat, provider-facing classification; namespace is never model-owned."""

    intent: Literal["diagnose", "clarify"]
    domain: Literal["kubernetes", "microservice", "unknown"]
    fault_family: FaultCandidateV2
    target_kind: TargetKindCandidateV2
    resource: str = Field(max_length=253)


class V2Target(StrictSchema):
    namespace: NamespaceName
    kind: TargetKindV2
    resource: ResourceName


class IntentV2(StrictSchema):
    schema_version: Literal[2] = 2
    intent: Literal["diagnose"] = "diagnose"
    domain: Literal["kubernetes", "microservice"]
    fault_family: FaultFamilyV2
    target: V2Target


class RouterOutcomeV2(StrictSchema):
    intent: IntentV2 | None
    clarification_code: Literal["target_required", "target_ambiguous"] | None
    budget: BudgetState
    attempts: int = Field(ge=1, le=3)
    llm_call_ids: tuple[str, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def require_one_result(self) -> RouterOutcomeV2:
        if (self.intent is None) == (self.clarification_code is None):
            raise ValueError("routing must produce intent or clarification")
        return self


class V2IntentRouter:
    """Treat the model's fault family as a scoped hypothesis, not a fact."""

    def __init__(
        self,
        *,
        client: StructuredModelClient,
        audit_repository: ModelAuditRepository,
        prompt: PromptTemplate,
        model_config: StructuredModelConfig,
        settings: RouterSettings | None = None,
    ) -> None:
        if prompt.component != "router" or prompt.version != "v2":
            raise ValueError("V2IntentRouter requires router/v2 prompt")
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
    ) -> RouterOutcomeV2:
        try:
            router_input = RouterInput(
                run_id=run_id, query=query, namespace=namespace
            )
        except ValidationError:
            raise RouterScopeError("router input is invalid") from None
        ensure_model_budget(budget)
        try:
            prompt_version_id = self._audit.register_prompt(self._prompt)
        except ModelAuditError:
            raise ModelExternalError(
                "model audit prompt could not be registered"
            ) from None

        user_data = json.dumps(
            {"namespace": router_input.namespace, "query": router_input.query},
            ensure_ascii=True, separators=(",", ":"),
        )
        messages = [
            ModelMessage(role=ModelRole.SYSTEM, content=self._prompt.content),
            ModelMessage(role=ModelRole.USER, content=user_data),
        ]
        call_ids: list[str] = []
        current_budget = budget

        for attempt_index in range(self._settings.max_schema_retries + 1):
            ensure_model_budget(current_budget)
            request = StructuredModelRequest(
                messages=tuple(messages),
                prompt=PromptReference.from_template(self._prompt),
                config=self._model_config,
            )
            started_at = datetime.now(UTC)
            started_clock = perf_counter()
            try:
                result = await self._client.complete(request, V2RouterModelOutput)
            except ModelGatewayError as exc:
                elapsed_ms = max(0, round((perf_counter() - started_clock) * 1_000))
                usage = exc.usage or ModelUsage()
                latency_ms = (
                    exc.latency_ms if exc.latency_ms is not None else elapsed_ms
                )
                call_ids.append(
                    self._append_attempt(
                        router_input=router_input,
                        prompt_version_id=prompt_version_id,
                        attempt_index=attempt_index,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        usage=usage,
                        latency_ms=latency_ms,
                        model_version=exc.model_version,
                        output=None,
                        error=exc,
                    )
                )
                current_budget = consume_usage(
                    current_budget, usage=usage, latency_ms=latency_ms
                )
                if not isinstance(exc, ModelSchemaError):
                    raise
                if attempt_index >= self._settings.max_schema_retries:
                    raise
                if current_budget.retries_used >= current_budget.limits.max_retries:
                    raise ModelBudgetError(
                        "schema regeneration retry budget is exhausted"
                    ) from None
                current_budget = consume_retry(current_budget)
                messages.append(
                    ModelMessage(
                        role=ModelRole.DEVELOPER, content=_REGENERATION_MESSAGE
                    )
                )
                continue

            current_budget = consume_usage(
                current_budget,
                usage=result.metadata.usage,
                latency_ms=result.metadata.latency_ms,
            )
            try:
                intent, clarification = _apply_scope(result.output, router_input)
            except RouterScopeError as exc:
                call_ids.append(
                    self._append_attempt(
                        router_input=router_input,
                        prompt_version_id=prompt_version_id,
                        attempt_index=attempt_index,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        usage=result.metadata.usage,
                        latency_ms=result.metadata.latency_ms,
                        model_version=result.metadata.model_version,
                        output=result.output,
                        error=exc,
                    )
                )
                raise
            call_ids.append(
                self._append_attempt(
                    router_input=router_input,
                    prompt_version_id=prompt_version_id,
                    attempt_index=attempt_index,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    usage=result.metadata.usage,
                    latency_ms=result.metadata.latency_ms,
                    model_version=result.metadata.model_version,
                    output=result.output,
                    error=None,
                )
            )
            return RouterOutcomeV2(
                intent=intent,
                clarification_code=clarification,
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
        output: V2RouterModelOutput | None,
        error: ModelGatewayError | None,
    ) -> str:
        request_payload: dict[str, JsonValue] = {
            "namespace": router_input.namespace,
            "query_sha256": sha256(router_input.query.encode("utf-8")).hexdigest(),
            "query_length": len(router_input.query),
            "output_schema": V2RouterModelOutput.__name__,
            "attempt": attempt_index + 1,
        }
        response_payload: dict[str, JsonValue] | None = None
        if output is not None:
            response_payload = {
                "intent": output.intent,
                "domain": output.domain,
                "fault_family": output.fault_family,
                "target_kind": output.target_kind,
                "resource_sha256": sha256(output.resource.encode("utf-8")).hexdigest(),
                "resource_length": len(output.resource),
            }
        try:
            recorded = self._audit.append_attempt(
                ModelCallAttempt(
                    run_id=router_input.run_id,
                    component="router",
                    prompt_version_id=prompt_version_id,
                    provider=self._model_config.provider,
                    model_name=self._model_config.model,
                    model_version=model_version or self._model_config.model_version,
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


def _apply_scope(
    output: V2RouterModelOutput,
    router_input: RouterInput,
) -> tuple[
    IntentV2 | None,
    Literal["target_required", "target_ambiguous"] | None,
]:
    if output.intent == "clarify" or output.target_kind == "unknown":
        return None, "target_required"
    if output.domain == "unknown" or output.fault_family == "unknown":
        raise RouterScopeError("router output is outside V2 diagnosis scope")
    if not output.resource:
        return None, "target_required"
    try:
        target = V2Target(
            namespace=router_input.namespace,
            kind=output.target_kind,
            resource=output.resource,
        )
    except ValidationError:
        raise RouterScopeError("router target is invalid") from None
    if output.fault_family in _POD_FAMILIES:
        if target.kind not in {"pod", "deployment"}:
            return None, "target_ambiguous"
    elif output.fault_family == "service_503":
        if target.kind not in {"service", "ingress"}:
            return None, "target_ambiguous"
    elif target.kind not in {"deployment", "service", "ingress"}:
        return None, "target_ambiguous"
    return (
        IntentV2(
            domain=output.domain,
            fault_family=output.fault_family,
            target=target,
        ),
        None,
    )
