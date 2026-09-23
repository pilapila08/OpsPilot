"""Audited model planner with bounded schema and semantic regeneration."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter

from pydantic import JsonValue

from opspilot.agent.schemas import BudgetState, ExecutionPlanV1
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.llm.audit import ModelAuditError, ModelAuditRepository, ModelCallAttempt
from opspilot.llm.budget import consume_retry, consume_usage, ensure_model_budget
from opspilot.llm.client import StructuredModelClient
from opspilot.llm.errors import (
    ModelBudgetError,
    ModelExternalError,
    ModelGatewayError,
    ModelSchemaError,
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
from opspilot.planning.models import PlannerOutcome, PlannerSettings
from opspilot.planning.validator import PlanValidator

_SCHEMA_REGENERATION = (
    "The prior plan failed the requested JSON schema. Return a new plan "
    "matching the schema exactly; do not repeat the earlier response."
)


class PlanRejectedError(RuntimeError):
    """A stable validation failure after bounded semantic regeneration."""

    def __init__(self, error: ErrorInfo) -> None:
        self.error = error
        super().__init__(error.message)


class V1Planner:
    """Generate a plan from descriptors and admit it through PlanValidator."""

    def __init__(
        self,
        *,
        client: StructuredModelClient,
        audit_repository: ModelAuditRepository,
        prompt: PromptTemplate,
        model_config: StructuredModelConfig,
        settings: PlannerSettings | None = None,
    ) -> None:
        if prompt.component != "planner":
            raise ValueError("V1Planner requires a planner prompt")
        self._client = client
        self._audit = audit_repository
        self._prompt = prompt
        self._model_config = model_config
        self._settings = settings or PlannerSettings()

    async def plan(
        self,
        *,
        run_id: str,
        state: AgentState,
        validator: PlanValidator,
    ) -> PlannerOutcome:
        if (
            state.status is not AgentStatus.PLANNING
            or state.intent is None
            or state.plan is not None
            or state.execution_plan_v1 is not None
        ):
            raise PlanRejectedError(
                ErrorInfo.from_code(
                    ErrorCode.POLICY_REJECTED,
                    "planner requires a routed state in PLANNING without a plan",
                )
            )
        ensure_model_budget(state.budget)
        try:
            prompt_version_id = self._audit.register_prompt(self._prompt)
        except ModelAuditError:
            raise ModelExternalError("model audit prompt could not be registered") from None

        messages = _initial_messages(self._prompt, state, validator)
        budget = state.budget
        call_ids: list[str] = []
        schema_retries = 0
        plan_retries = 0

        while True:
            ensure_model_budget(budget)
            attempt_index = len(call_ids)
            request = StructuredModelRequest(
                messages=tuple(messages),
                prompt=PromptReference.from_template(self._prompt),
                config=self._model_config,
            )
            started_at = datetime.now(UTC)
            started_clock = perf_counter()
            try:
                result = await self._client.complete(request, ExecutionPlanV1)
            except ModelGatewayError as exc:
                completed_at = datetime.now(UTC)
                latency_ms = exc.latency_ms if exc.latency_ms is not None else max(
                    0, round((perf_counter() - started_clock) * 1_000)
                )
                usage = exc.usage or ModelUsage()
                call_ids.append(
                    self._append_attempt(
                        run_id=run_id,
                        state=state,
                        prompt_version_id=prompt_version_id,
                        attempt_index=attempt_index,
                        started_at=started_at,
                        completed_at=completed_at,
                        usage=usage,
                        latency_ms=latency_ms,
                        model_version=exc.model_version,
                        plan=None,
                        error_code=exc.code,
                    )
                )
                budget = consume_usage(budget, usage=usage, latency_ms=latency_ms)
                if not isinstance(exc, ModelSchemaError) or schema_retries >= self._settings.max_schema_retries:
                    raise
                schema_retries += 1
                budget = consume_retry(budget)
                messages.append(ModelMessage(role=ModelRole.DEVELOPER, content=_SCHEMA_REGENERATION))
                continue

            completed_at = datetime.now(UTC)
            budget = consume_usage(
                budget,
                usage=result.metadata.usage,
                latency_ms=result.metadata.latency_ms,
            )
            validation = validator.validate(result.output, intent=state.intent, budget=budget)
            error = validation if isinstance(validation, ErrorInfo) else None
            call_ids.append(
                self._append_attempt(
                    run_id=run_id,
                    state=state,
                    prompt_version_id=prompt_version_id,
                    attempt_index=attempt_index,
                    started_at=started_at,
                    completed_at=completed_at,
                    usage=result.metadata.usage,
                    latency_ms=result.metadata.latency_ms,
                    model_version=result.metadata.model_version,
                    plan=result.output,
                    error_code=error.code if error is not None else None,
                )
            )
            if error is None:
                assert not isinstance(validation, ErrorInfo)
                return PlannerOutcome(
                    state=state.with_execution_plan(validation.plan, budget),
                    validated_plan=validation,
                    attempts=len(call_ids),
                    llm_call_ids=tuple(call_ids),
                )
            if error.code is ErrorCode.BUDGET_EXCEEDED:
                raise ModelBudgetError(error.message)
            if plan_retries >= self._settings.max_plan_retries:
                raise PlanRejectedError(error)
            plan_retries += 1
            budget = consume_retry(budget)
            messages.append(
                ModelMessage(
                    role=ModelRole.DEVELOPER,
                    content=(
                        "The prior plan was rejected by policy or input validation "
                        f"({error.code.value}). Return a new plan using only the "
                        "listed tools, authorized target and exact input schemas. "
                        "Do not repeat the prior plan."
                    ),
                )
            )

    def _append_attempt(
        self,
        *,
        run_id: str,
        state: AgentState,
        prompt_version_id: str,
        attempt_index: int,
        started_at: datetime,
        completed_at: datetime,
        usage: ModelUsage,
        latency_ms: int,
        model_version: str | None,
        plan: ExecutionPlanV1 | None,
        error_code: ErrorCode | None,
    ) -> str:
        assert state.intent is not None
        request_payload: dict[str, JsonValue] = {
            "namespace": state.intent.target.namespace,
            "resource": state.intent.target.resource,
            "output_schema": ExecutionPlanV1.__name__,
            "attempt": attempt_index + 1,
        }
        response_payload: dict[str, JsonValue] | None = None
        if plan is not None:
            response_payload = {
                "plan_sha256": sha256(plan.model_dump_json().encode("utf-8")).hexdigest(),
                "step_count": len(plan.steps),
                "tools": [step.tool for step in plan.steps],
                "validation_error_code": error_code.value if error_code is not None else None,
                "security_event": error_code in {ErrorCode.TOOL_NOT_FOUND, ErrorCode.POLICY_REJECTED},
            }
        try:
            recorded = self._audit.append_attempt(
                ModelCallAttempt(
                    run_id=run_id,
                    component="planner",
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
                    success=error_code is None,
                    error_code=error_code,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        except ModelAuditError:
            raise ModelExternalError("model call audit could not be persisted") from None
        return recorded.call_id


def _initial_messages(
    prompt: PromptTemplate,
    state: AgentState,
    validator: PlanValidator,
) -> list[ModelMessage]:
    assert state.intent is not None
    descriptors = [item.model_dump(mode="json") for item in validator.descriptors]
    payload = json.dumps(
        {
            "intent": state.intent.model_dump(mode="json"),
            "tool_descriptors": descriptors,
            "budget": {
                "steps_remaining": state.budget.limits.max_steps - state.budget.steps_used,
                "tool_calls_remaining": state.budget.limits.max_tool_calls - state.budget.tool_calls_used,
                "retries_remaining": state.budget.limits.max_retries - state.budget.retries_used,
                "seconds_remaining": state.budget.limits.timeout_seconds - state.budget.elapsed_seconds,
            },
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        ModelMessage(role=ModelRole.SYSTEM, content=prompt.content),
        ModelMessage(role=ModelRole.USER, content=payload),
    ]
