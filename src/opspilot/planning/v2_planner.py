"""Audited, bounded V2 round planning over sanitized observations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter

from pydantic import JsonValue

from opspilot.agent.schemas import BudgetState, StrictSchema
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.llm.audit import ModelAuditError, ModelAuditRepository, ModelCallAttempt
from opspilot.llm.budget import (
    complete_with_budget, consume_retry, ensure_model_budget, ensure_planner_budget,
)
from opspilot.llm.client import StructuredModelClient
from opspilot.llm.errors import ModelBudgetError, ModelExternalError, ModelGatewayError, ModelSchemaError
from opspilot.llm.models import (
    ModelMessage, ModelRole, ModelUsage, PromptReference, PromptTemplate,
    StructuredModelConfig, StructuredModelRequest,
)
from opspilot.planning.v2 import (
    AdmittedDecisionV2, ObservationSummaryV2, PlanDecisionV2, V2PlanValidator,
)
from opspilot.routing.v2 import IntentV2

_SCHEMA_RETRY = "The prior response did not match PlanDecisionV2. Return exact fields and enum values."
_SEMANTIC_RETRY = "The prior decision was rejected by policy. Use only scoped read-only Tools and current Evidence IDs."


class V2PlannerOutcome(StrictSchema):
    admitted: AdmittedDecisionV2
    budget: BudgetState
    prompt_version_id: str
    llm_call_ids: tuple[str, ...]


class V2PlanningError(RuntimeError):
    def __init__(self, error: ErrorInfo, budget: BudgetState) -> None:
        self.error = error
        self.budget = budget
        super().__init__(error.message)


class V2Planner:
    def __init__(
        self, *, client: StructuredModelClient,
        audit_repository: ModelAuditRepository, prompt: PromptTemplate,
        model_config: StructuredModelConfig,
        max_schema_retries: int = 2, max_semantic_retries: int = 1,
    ) -> None:
        if prompt.component != "planner" or prompt.version != "v2":
            raise ValueError("V2Planner requires planner/v2 prompt")
        if not 0 <= max_schema_retries <= 2 or not 0 <= max_semantic_retries <= 1:
            raise ValueError("V2 planner retries exceed policy")
        self._client = client
        self._audit = audit_repository
        self._prompt = prompt
        self._config = model_config
        self._schema_retries = max_schema_retries
        self._semantic_retries = max_semantic_retries

    async def plan(
        self, *, run_id: str, intent: IntentV2, observation: ObservationSummaryV2,
        round_no: int, budget: BudgetState, validator: V2PlanValidator,
        used_call_ids: frozenset[str],
        used_requests: frozenset[tuple[str, str]],
        allowed_resources: frozenset[str],
    ) -> V2PlannerOutcome:
        if round_no < 1 or round_no > 4:
            raise ValueError("V2 round is out of bounds")
        if intent.target.resource not in allowed_resources:
            raise V2PlanningError(
                ErrorInfo.from_code(ErrorCode.POLICY_REJECTED, "target exceeds allowed resources"),
                budget,
            )
        try:
            ensure_planner_budget(budget)
            prompt_id = self._audit.register_prompt(self._prompt)
        except ModelGatewayError as exc:
            raise V2PlanningError(exc.to_error_info(retryable=False), budget) from None
        except ModelAuditError:
            raise V2PlanningError(
                ErrorInfo.from_code(ErrorCode.EXTERNAL_SERVICE_ERROR, "prompt audit failed"), budget
            ) from None

        payload = json.dumps({
            "intent": intent.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
            "round_no": round_no,
            "allowed_resources": sorted(allowed_resources),
            "tool_descriptors": [item.model_dump(mode="json") for item in validator.descriptors],
            "budget": {
                "steps_remaining": budget.limits.max_steps - budget.steps_used,
                "tool_calls_remaining": budget.limits.max_tool_calls - budget.tool_calls_used,
                "retries_remaining": budget.limits.max_retries - budget.retries_used,
                "seconds_remaining": budget.limits.timeout_seconds - budget.elapsed_seconds,
            },
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        messages = [
            ModelMessage(role=ModelRole.SYSTEM, content=self._prompt.content),
            ModelMessage(role=ModelRole.USER, content=payload),
        ]
        current = budget
        call_ids: list[str] = []
        schema_retries = 0
        semantic_retries = 0
        while True:
            try:
                ensure_planner_budget(current)
            except ModelBudgetError as exc:
                raise V2PlanningError(exc.to_error_info(retryable=False), current) from None
            request = StructuredModelRequest(
                messages=tuple(messages), prompt=PromptReference.from_template(self._prompt),
                config=self._config,
            )
            started_at = datetime.now(UTC)
            started_clock = perf_counter()
            try:
                result, current = await complete_with_budget(
                    self._client, request, PlanDecisionV2, current, phase="planner",
                )
            except ModelGatewayError as exc:
                latency_ms = exc.latency_ms if exc.latency_ms is not None else max(
                    0, round((perf_counter() - started_clock) * 1_000)
                )
                usage = exc.usage or ModelUsage()
                current = exc.budget or current
                call_ids.append(self._record(
                    run_id=run_id, prompt_id=prompt_id, round_no=round_no,
                    attempt_index=len(call_ids), started_at=started_at,
                    usage=usage, latency_ms=latency_ms, model_version=exc.model_version,
                    decision=None, error_code=exc.code, budget=current,
                ))
                if exc.budget_stop is not None:
                    raise V2PlanningError(exc.to_error_info(retryable=False), current) from None
                try:
                    ensure_model_budget(current, phase="planner.after", after=True)
                except ModelBudgetError as budget_exc:
                    raise V2PlanningError(budget_exc.to_error_info(retryable=False), current) from None
                if not isinstance(exc, ModelSchemaError) or schema_retries >= self._schema_retries:
                    raise V2PlanningError(exc.to_error_info(retryable=False), current) from None
                try:
                    current = consume_retry(current, phase="planner.retry")
                except ModelBudgetError as budget_exc:
                    raise V2PlanningError(budget_exc.to_error_info(retryable=False), current) from None
                schema_retries += 1
                messages.append(ModelMessage(role=ModelRole.DEVELOPER, content=_SCHEMA_RETRY))
                continue

            admitted = validator.validate(
                result.output, intent=intent, round_no=round_no,
                evidence_ids=observation.evidence_ids,
                used_call_ids=used_call_ids, used_requests=used_requests,
                allowed_resources=allowed_resources, budget=current,
            )
            error = admitted if isinstance(admitted, ErrorInfo) else None
            call_ids.append(self._record(
                run_id=run_id, prompt_id=prompt_id, round_no=round_no,
                attempt_index=len(call_ids), started_at=started_at,
                usage=result.metadata.usage,
                latency_ms=result.metadata.latency_ms,
                model_version=result.metadata.model_version,
                decision=result.output, error_code=error.code if error else None,
                budget=current,
            ))
            try:
                ensure_model_budget(current, phase="planner.after", after=True)
            except ModelBudgetError as exc:
                raise V2PlanningError(exc.to_error_info(retryable=False), current) from None
            if error is None:
                assert isinstance(admitted, AdmittedDecisionV2)
                return V2PlannerOutcome(
                    admitted=admitted, budget=current,
                    prompt_version_id=prompt_id, llm_call_ids=tuple(call_ids),
                )
            if error.code is ErrorCode.BUDGET_EXCEEDED or semantic_retries >= self._semantic_retries:
                raise V2PlanningError(error, current)
            try:
                current = consume_retry(current, phase="planner.retry")
            except ModelBudgetError as exc:
                raise V2PlanningError(exc.to_error_info(retryable=False), current) from None
            semantic_retries += 1
            messages.append(ModelMessage(role=ModelRole.DEVELOPER, content=_SEMANTIC_RETRY))

    def _record(
        self, *, run_id: str, prompt_id: str, round_no: int, attempt_index: int,
        started_at: datetime, usage: ModelUsage, latency_ms: int,
        model_version: str | None, decision: PlanDecisionV2 | None,
        error_code: ErrorCode | None, budget: BudgetState,
    ) -> str:
        response: dict[str, JsonValue] | None = None
        if decision is not None:
            response = {
                "decision_sha256": sha256(decision.model_dump_json().encode("utf-8")).hexdigest(),
                "action": decision.action,
                "tools": [call.tool for call in decision.calls],
                "validation_error_code": error_code.value if error_code else None,
            }
        try:
            recorded = self._audit.append_attempt(ModelCallAttempt(
                run_id=run_id, component="planner", prompt_version_id=prompt_id,
                provider=self._config.provider, model_name=self._config.model,
                model_version=model_version or self._config.model_version,
                request_payload={
                    "round_no": round_no, "attempt": attempt_index + 1,
                    "output_schema": "PlanDecisionV2",
                },
                response_payload=response,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd, latency_ms=latency_ms,
                retry_count=attempt_index, success=error_code is None,
                error_code=error_code, started_at=started_at,
                completed_at=datetime.now(UTC),
            ))
        except ModelAuditError:
            raise V2PlanningError(
                ErrorInfo.from_code(ErrorCode.EXTERNAL_SERVICE_ERROR, "model audit failed"),
                budget,
            ) from None
        return recorded.call_id
