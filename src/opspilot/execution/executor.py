"""Sequential, budgeted Tool execution over an admitted V1 plan."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from opspilot.agent.schemas import BudgetState
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.errors import ErrorCode, ErrorInfo, error_policy
from opspilot.evidence import EvidenceExtractionError, EvidenceExtractorRegistry
from opspilot.evidence.models import Evidence
from opspilot.execution.models import ExecutionSummary, ToolAttempt
from opspilot.execution.repository import ExecutionPersistenceError, ExecutionRepository
from opspilot.planning import PlanValidator, ValidatedPlanV1
from opspilot.tools.models import ToolInvocation, ToolMetadata, ToolResponse, ToolRiskLevel
from opspilot.tools.registry import ToolNotRegisteredError, ToolRegistry


class ExecutionRejectedError(ValueError):
    """Executor input does not match an admitted state and plan."""


class BoundedExecutor:
    """Invoke only validated calls through the Registry and atomically record each attempt."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        repository: ExecutionRepository,
        extractors: EvidenceExtractorRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tool_attempt_id_factory: Callable[[], str] | None = None,
        evidence_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._registry = registry
        self._repository = repository
        self._extractors = extractors or EvidenceExtractorRegistry()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock
        self._sleep = sleep
        self._tool_attempt_id = tool_attempt_id_factory or (lambda: f"tool_{uuid4().hex}")
        self._evidence_id = evidence_id_factory or (lambda: f"ev_{uuid4().hex}")

    async def execute(
        self,
        *,
        run_id: str,
        state: AgentState,
        validated_plan: ValidatedPlanV1,
    ) -> ExecutionSummary:
        if not isinstance(validated_plan, ValidatedPlanV1):
            raise ExecutionRejectedError("Executor requires a validated V1 plan")
        if (
            state.status is not AgentStatus.PLANNING
            or state.intent is None
            or state.execution_plan_v1 is None
            or state.execution_plan_v1.model_dump_json() != validated_plan.plan_json
        ):
            raise ExecutionRejectedError("Executor state does not match the validated plan")
        plan = validated_plan.plan
        admission = PlanValidator(self._registry).validate(
            plan, intent=state.intent, budget=state.budget
        )
        if isinstance(admission, ErrorInfo):
            return self._initial_rejection(state, admission)
        if admission.tool_versions != validated_plan.tool_versions:
            return self._initial_rejection(
                state,
                ErrorInfo.from_code(
                    ErrorCode.POLICY_REJECTED,
                    "Tool registry changed after plan validation",
                ),
            )

        executing = state.transition_to(AgentStatus.EXECUTING, at=self._clock())
        start_monotonic = self._monotonic()
        base_elapsed = state.budget.elapsed_seconds
        budget = state.budget
        call_ids = list(state.tool_call_ids)
        evidence_ids = list(state.evidence_ids)
        completed_steps = 0

        for step in plan.steps:
            budget = _with_elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start_monotonic))
            if budget.steps_used >= budget.limits.max_steps or budget.tool_calls_used >= budget.limits.max_tool_calls or budget.elapsed_seconds >= budget.limits.timeout_seconds:
                return self._finish(
                    executing, budget, completed_steps, call_ids, evidence_ids,
                    _error(ErrorCode.BUDGET_EXCEEDED, "execution budget is exhausted"),
                )
            try:
                definition = self._registry.get(step.tool)
            except ToolNotRegisteredError:
                return self._finish(
                    executing, budget, completed_steps, call_ids, evidence_ids,
                    _error(ErrorCode.TOOL_NOT_FOUND, "validated Tool is no longer registered"),
                )
            if definition.risk_level is not ToolRiskLevel.READ_ONLY or definition.version != validated_plan.tool_versions[step.step_id - 1][1]:
                return self._finish(
                    executing, budget, completed_steps, call_ids, evidence_ids,
                    _error(ErrorCode.POLICY_REJECTED, "validated Tool policy or version changed"),
                )

            invocation = ToolInvocation(
                call_id=step.call_id,
                tool=step.tool,
                arguments=step.arguments,
            )
            attempt_no = 0
            while True:
                budget = _with_elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start_monotonic))
                if budget.tool_calls_used >= budget.limits.max_tool_calls or budget.elapsed_seconds >= budget.limits.timeout_seconds:
                    return self._finish(
                        executing, budget, completed_steps, call_ids, evidence_ids,
                        _error(ErrorCode.BUDGET_EXCEEDED, "execution budget is exhausted"),
                    )
                attempt_no += 1
                started_at = self._clock()
                remaining = budget.limits.timeout_seconds - budget.elapsed_seconds
                try:
                    async with asyncio.timeout(remaining):
                        response = await self._registry.invoke(invocation)
                except TimeoutError:
                    response = _failure_response(
                        invocation, definition.version, ErrorCode.BUDGET_EXCEEDED,
                        "runtime execution timed out",
                    )
                completed_at = self._clock()
                budget = _with_elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start_monotonic))
                budget = _with_counts(
                    budget,
                    steps=budget.steps_used + (1 if attempt_no == 1 else 0),
                    calls=budget.tool_calls_used + 1,
                )
                if response.metadata.call_id != invocation.call_id or response.metadata.tool_name != invocation.tool or response.metadata.tool_version != definition.version:
                    response = _failure_response(
                        invocation, definition.version, ErrorCode.TOOL_OUTPUT_INVALID,
                        "Tool response identity or version is invalid",
                    )

                record_id = self._tool_attempt_id()
                evidence: tuple[Evidence, ...] = ()
                if response.success:
                    try:
                        evidence = self._extractors.extract(
                            invocation=invocation,
                            response=response,
                            trace_id=state.trace_id,
                            tool_attempt_id=record_id,
                            collected_at=completed_at,
                            evidence_id_factory=self._evidence_id,
                        )
                    except EvidenceExtractionError:
                        response = _failure_response(
                            invocation, definition.version, ErrorCode.TOOL_OUTPUT_INVALID,
                            "Tool data could not be safely extracted",
                        )

                attempt = ToolAttempt(
                    record_id=record_id,
                    run_id=run_id,
                    trace_id=state.trace_id,
                    invocation=invocation,
                    attempt_no=attempt_no,
                    risk_level=definition.risk_level,
                    started_at=started_at,
                    completed_at=completed_at,
                    response=response,
                )
                try:
                    recorded = self._repository.append_attempt(attempt, evidence)
                except ExecutionPersistenceError:
                    return self._finish(
                        executing, budget, completed_steps, call_ids, evidence_ids,
                        _error(ErrorCode.EXTERNAL_SERVICE_ERROR, "Tool audit could not be persisted"),
                    )
                call_ids.append(recorded.record_id)
                evidence_ids.extend(item.evidence_id for item in evidence)

                if response.success:
                    completed_steps += 1
                    break
                assert response.error is not None
                code = response.error.code
                retry_allowed = (
                    response.error.retryable
                    and error_policy(code).retryable
                    and definition.retry_policy.permits(code)
                    and attempt_no <= definition.retry_policy.max_retries
                    and attempt_no <= error_policy(code).max_retries
                )
                if not retry_allowed:
                    return self._finish(
                        executing, budget, completed_steps, call_ids, evidence_ids,
                        response.error,
                    )
                if budget.retries_used >= budget.limits.max_retries or budget.tool_calls_used >= budget.limits.max_tool_calls or budget.elapsed_seconds >= budget.limits.timeout_seconds:
                    return self._finish(
                        executing, budget, completed_steps, call_ids, evidence_ids,
                        _error(ErrorCode.BUDGET_EXCEEDED, "Tool retry budget is exhausted"),
                    )
                budget = _with_retry(budget)
                backoff = min(
                    definition.retry_policy.initial_backoff_seconds
                    * definition.retry_policy.backoff_multiplier ** (attempt_no - 1),
                    definition.retry_policy.max_backoff_seconds,
                )
                try:
                    async with asyncio.timeout(budget.limits.timeout_seconds - budget.elapsed_seconds):
                        await self._sleep(backoff)
                except TimeoutError:
                    budget = _with_elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start_monotonic))
                    return self._finish(
                        executing, budget, completed_steps, call_ids, evidence_ids,
                        _error(ErrorCode.BUDGET_EXCEEDED, "Tool retry backoff exceeded runtime budget"),
                    )

        budget = _with_elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start_monotonic))
        if budget.elapsed_seconds >= budget.limits.timeout_seconds:
            return self._finish(
                executing, budget, completed_steps, call_ids, evidence_ids,
                _error(ErrorCode.BUDGET_EXCEEDED, "runtime execution timed out"),
            )
        finished = _progress(executing, budget, completed_steps, call_ids, evidence_ids)
        return ExecutionSummary(
            state=finished.transition_to(AgentStatus.VERIFYING, at=self._clock()),
            completed_steps=completed_steps,
            tool_attempt_ids=tuple(call_ids),
            evidence_ids=tuple(evidence_ids),
        )

    @staticmethod
    def _initial_rejection(state: AgentState, error: ErrorInfo) -> ExecutionSummary:
        terminal = AgentStatus.BUDGET_EXCEEDED if error.code is ErrorCode.BUDGET_EXCEEDED else AgentStatus.POLICY_REJECTED
        return ExecutionSummary(
            state=state.transition_to(terminal),
            completed_steps=0,
            tool_attempt_ids=state.tool_call_ids,
            evidence_ids=state.evidence_ids,
            error=error,
        )

    def _finish(
        self,
        state: AgentState,
        budget: BudgetState,
        completed_steps: int,
        call_ids: list[str],
        evidence_ids: list[str],
        error: ErrorInfo,
    ) -> ExecutionSummary:
        progressed = _progress(state, budget, completed_steps, call_ids, evidence_ids)
        if error.code is ErrorCode.BUDGET_EXCEEDED:
            progressed = progressed.transition_to(AgentStatus.BUDGET_EXCEEDED, at=self._clock())
        elif error.code is ErrorCode.POLICY_REJECTED:
            progressed = progressed.transition_to(AgentStatus.POLICY_REJECTED, at=self._clock())
        elif not evidence_ids:
            progressed = progressed.transition_to(AgentStatus.FAILED, at=self._clock())
        return ExecutionSummary(
            state=progressed,
            completed_steps=completed_steps,
            tool_attempt_ids=tuple(call_ids),
            evidence_ids=tuple(evidence_ids),
            error=error,
        )


def _progress(
    state: AgentState,
    budget: BudgetState,
    completed_steps: int,
    call_ids: list[str],
    evidence_ids: list[str],
) -> AgentState:
    return AgentState.model_validate(
        {
            **state.model_dump(mode="python"),
            "budget": budget,
            "current_step": completed_steps,
            "tool_call_ids": tuple(call_ids),
            "evidence_ids": tuple(evidence_ids),
        }
    )


def _with_counts(budget: BudgetState, *, steps: int, calls: int) -> BudgetState:
    return BudgetState.model_validate(
        {**budget.model_dump(mode="python"), "steps_used": steps, "tool_calls_used": calls}
    )


def _with_elapsed(budget: BudgetState, elapsed: float) -> BudgetState:
    return BudgetState.model_validate(
        {**budget.model_dump(mode="python"), "elapsed_seconds": max(budget.elapsed_seconds, elapsed)}
    )


def _with_retry(budget: BudgetState) -> BudgetState:
    return BudgetState.model_validate(
        {**budget.model_dump(mode="python"), "retries_used": budget.retries_used + 1}
    )


def _error(code: ErrorCode, message: str) -> ErrorInfo:
    return ErrorInfo.from_code(code, message, retryable=False)


def _failure_response(
    invocation: ToolInvocation,
    version: str,
    code: ErrorCode,
    message: str,
) -> ToolResponse:
    return ToolResponse(
        success=False,
        data=None,
        metadata=ToolMetadata(
            call_id=invocation.call_id,
            tool_name=invocation.tool,
            source="registry",
            duration_ms=0,
            tool_version=version,
        ),
        error=_error(code, message),
    )
