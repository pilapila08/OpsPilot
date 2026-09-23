"""Single-run V1 orchestration over explicit model, Tool, and repository boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

from opspilot.agent.schemas import BudgetLimits, BudgetState
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.diagnosis import DiagnosisInputError, V1DiagnosisAssembler
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.execution import BoundedExecutor, ExecutionRejectedError, ExecutionSummary
from opspilot.llm.budget import ensure_model_budget
from opspilot.llm.errors import ModelBudgetError, ModelGatewayError
from opspilot.planning import PlanRejectedError, PlanValidator, V1Planner
from opspilot.routing import IntentRouter
from opspilot.runtime.models import DiagnosisRequest, DiagnosisRunResult
from opspilot.runtime.replay import ReplayConfigurationError
from opspilot.storage.contracts import (
    ResultRepository, RunRepository, RunSnapshot,
    RuntimePersistenceError, TaskRepository, TaskSnapshot,
)
from opspilot.execution.repository import ExecutionRepository
from opspilot.tools import ToolRegistry


class RuntimeIdFactory(Protocol):
    def new(self, kind: str) -> str: ...


class UuidRuntimeIds:
    def new(self, kind: str) -> str:
        return f"{kind}_{uuid4().hex}"


class DiagnosisRuntime:
    """Route, plan, execute, and verify one diagnosis without global state."""

    def __init__(
        self,
        *,
        router: IntentRouter,
        planner: V1Planner,
        diagnosis: V1DiagnosisAssembler,
        task_repository: TaskRepository,
        run_repository: RunRepository,
        tool_repository: ExecutionRepository,
        result_repository: ResultRepository,
        registry_factory: Callable[[DiagnosisRequest], ToolRegistry],
        budget_limits: BudgetLimits | None = None,
        ids: RuntimeIdFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._router = router
        self._planner = planner
        self._diagnosis = diagnosis
        self._tasks = task_repository
        self._runs = run_repository
        self._tools = tool_repository
        self._results = result_repository
        self._registry_factory = registry_factory
        self._limits = budget_limits or BudgetLimits()
        self._ids = ids or UuidRuntimeIds()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock
        self._sleep = sleep

    async def run(self, request: DiagnosisRequest) -> DiagnosisRunResult:
        started = self._monotonic()
        task_id = self._ids.new("task")
        run_id = self._ids.new("run")
        trace_id = self._ids.new("trace")
        now = self._clock()
        state = AgentState(
            task_id=task_id, trace_id=trace_id, user_query=request.query,
            budget=BudgetState(limits=self._limits),
            created_at=now, updated_at=now,
        )
        self._tasks.create_task(TaskSnapshot(
            task_id=task_id, user_query=request.query,
            namespace=request.namespace,
        ))
        self._runs.create_run(RunSnapshot(run_id=run_id, task_id=task_id, state=state))

        try:
            state = self._transition(state, AgentStatus.ROUTING)
            self._runs.save_state(run_id, state)
            registry = self._registry_factory(request)
            state = self._refresh_budget(state, started)
            ensure_model_budget(state.budget)

            routed = await self._router.route(
                run_id=run_id, query=request.query, namespace=request.namespace,
                budget=state.budget,
            )
            state = _with_state(state, intent=routed.intent, budget=routed.budget)
            state = self._refresh_budget(state, started)
            state = self._transition(state, AgentStatus.PLANNING)
            self._runs.save_state(run_id, state)
            ensure_model_budget(state.budget)

            planned = await self._planner.plan(
                run_id=run_id, state=state, validator=PlanValidator(registry),
            )
            state = self._refresh_budget(planned.state, started)
            self._runs.save_state(run_id, state)
            ensure_model_budget(state.budget)

            executor = BoundedExecutor(
                registry=registry, repository=self._tools,
                clock=self._clock, monotonic_clock=self._monotonic,
                sleep=self._sleep,
                tool_attempt_id_factory=lambda: self._ids.new("tool"),
                evidence_id_factory=lambda: self._ids.new("ev"),
                on_enter_executing=lambda executing: self._runs.save_state(run_id, executing),
            )
            execution = await executor.execute(
                run_id=run_id, state=state,
                validated_plan=planned.validated_plan,
            )
            state = self._refresh_budget(execution.state, started)
            self._runs.save_state(run_id, state)
            execution = execution.model_copy(update={"state": state})
            if state.status is AgentStatus.BUDGET_EXCEEDED and execution.evidence_ids:
                return self._budget_partial(run_id, state, execution)
            if state.status in {
                AgentStatus.FAILED, AgentStatus.BUDGET_EXCEEDED,
                AgentStatus.POLICY_REJECTED,
            }:
                return self._result(run_id, state, error=execution.error)
            if not execution.evidence_ids:
                return self._fail(
                    run_id, state,
                    ErrorInfo.from_code(
                        ErrorCode.SCHEMA_VALIDATION,
                        "execution produced no verifiable Evidence",
                        retryable=False,
                    ),
                )
            try:
                ensure_model_budget(state.budget)
            except ModelBudgetError:
                error = ErrorInfo.from_code(
                    ErrorCode.BUDGET_EXCEEDED,
                    "model budget is exhausted before diagnosis",
                    retryable=False,
                )
                state = self._transition(state, AgentStatus.BUDGET_EXCEEDED)
                self._runs.save_state(run_id, state)
                exhausted = execution.model_copy(update={"state": state, "error": error})
                return self._budget_partial(run_id, state, exhausted)

            diagnosed = await self._diagnosis.diagnose(
                run_id=run_id, result_id=self._ids.new("result"),
                execution=execution,
            )
            state = _with_state(
                state, budget=diagnosed.budget,
                diagnosis_id=diagnosed.result_id,
                verification_id=f"verification_{diagnosed.result_id}",
            )
            state = self._refresh_budget(state, started)
            terminal = (
                AgentStatus.COMPLETED
                if diagnosed.assessment.status == "COMPLETED"
                else AgentStatus.PARTIAL
            )
            state = self._transition(state, terminal)
            self._runs.save_state(run_id, state)
            return self._result(run_id, state, error=execution.error)
        except PlanRejectedError as exc:
            return self._fail(run_id, state, exc.error)
        except ModelGatewayError as exc:
            return self._fail(run_id, state, exc.to_error_info(retryable=False))
        except (ReplayConfigurationError, ExecutionRejectedError, DiagnosisInputError):
            return self._fail(
                run_id, state,
                ErrorInfo.from_code(
                    ErrorCode.POLICY_REJECTED,
                    "runtime input or replay policy was rejected",
                ),
            )
        except RuntimePersistenceError:
            return self._fail(
                run_id, state,
                ErrorInfo.from_code(
                    ErrorCode.EXTERNAL_SERVICE_ERROR,
                    "runtime record could not be persisted",
                    retryable=False,
                ),
            )
        except Exception:
            return self._fail(
                run_id, state,
                ErrorInfo.from_code(
                    ErrorCode.EXTERNAL_SERVICE_ERROR,
                    "runtime stage failed",
                    retryable=False,
                ),
            )

    def _fail(self, run_id: str, state: AgentState, error: ErrorInfo) -> DiagnosisRunResult:
        latest = self._runs.get_run(run_id)
        if latest is not None:
            state = latest.state
        if not state.status.is_terminal:
            terminal = _error_status(error.code)
            state = self._transition(state, terminal)
            self._runs.save_state(run_id, state)
        return self._result(run_id, state, error=error)

    def _budget_partial(
        self, run_id: str, state: AgentState, execution: ExecutionSummary,
    ) -> DiagnosisRunResult:
        diagnosed = self._diagnosis.partial_without_model(
            run_id=run_id, result_id=self._ids.new("result"),
            execution=execution,
        )
        state = _with_state(
            state, diagnosis_id=diagnosed.result_id,
            verification_id=f"verification_{diagnosed.result_id}",
        )
        self._runs.save_state(run_id, state)
        return self._result(run_id, state, error=execution.error)

    def _result(
        self, run_id: str, state: AgentState, *, error: ErrorInfo | None,
    ) -> DiagnosisRunResult:
        return DiagnosisRunResult(
            task_id=state.task_id, run_id=run_id, trace_id=state.trace_id,
            status=state.status, state=state,
            diagnosis=self._results.get_result(run_id), error=error,
        )

    def _transition(self, state: AgentState, target: AgentStatus) -> AgentState:
        return state.transition_to(target, at=max(self._clock(), state.updated_at))

    def _refresh_budget(self, state: AgentState, started: float) -> AgentState:
        elapsed = max(state.budget.elapsed_seconds, self._monotonic() - started)
        budget = BudgetState.model_validate({
            **state.budget.model_dump(mode="python"),
            "elapsed_seconds": elapsed,
        })
        return _with_state(state, budget=budget)


def _with_state(state: AgentState, **changes: object) -> AgentState:
    return AgentState.model_validate({**state.model_dump(mode="python"), **changes})


def _error_status(code: ErrorCode) -> AgentStatus:
    if code is ErrorCode.BUDGET_EXCEEDED:
        return AgentStatus.BUDGET_EXCEEDED
    if code in {ErrorCode.POLICY_REJECTED, ErrorCode.TOOL_NOT_FOUND}:
        return AgentStatus.POLICY_REJECTED
    return AgentStatus.FAILED
