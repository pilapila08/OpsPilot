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
from opspilot.budget import BudgetExceeded, BudgetManager, BudgetRejection, BudgetStopReason
from opspilot.diagnosis import DiagnosisInputError, V1DiagnosisAssembler
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.execution import BoundedExecutor, ExecutionRejectedError
from opspilot.llm.errors import ModelGatewayError
from opspilot.planning import PlanRejectedError, PlanValidator, V1Planner
from opspilot.routing import IntentRouter
from opspilot.runtime.models import DiagnosisRequest, DiagnosisRunResult
from opspilot.runtime.budget import persist_budget_partial
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

    async def run(
        self, request: DiagnosisRequest, *, task_id: str | None = None,
    ) -> DiagnosisRunResult:
        started = self._monotonic()
        if task_id is None:
            task_id = self._ids.new("task")
            self._tasks.create_task(TaskSnapshot(
                task_id=task_id, user_query=request.query,
                namespace=request.namespace, mode=request.mode, case_id=request.case_id,
            ))
        else:
            prepared = self._tasks.get_task(task_id)
            if (
                prepared is None or prepared.status is not AgentStatus.CREATED
                or prepared.user_query != request.query
                or prepared.namespace != request.namespace
                or prepared.mode != request.mode or prepared.case_id != request.case_id
            ):
                raise ValueError("prepared task does not match diagnosis request")
        run_id = self._ids.new("run")
        trace_id = self._ids.new("trace")
        now = self._clock()
        state = AgentState(
            task_id=task_id, trace_id=trace_id, user_query=request.query,
            budget=BudgetState(limits=self._limits),
            created_at=now, updated_at=now,
        )
        self._runs.create_run(RunSnapshot(run_id=run_id, task_id=task_id, state=state))

        try:
            state = self._transition(state, AgentStatus.ROUTING)
            self._runs.save_state(run_id, state)
            registry = self._registry_factory(request)
            state = self._refresh_budget(state, started)
            rejected = BudgetManager.check_model(state.budget, phase="router.before")
            if rejected is not None:
                raise BudgetExceeded(rejected)

            routed = await self._router.route(
                run_id=run_id, query=request.query, namespace=request.namespace,
                budget=state.budget,
            )
            state = _with_state(state, intent=routed.intent, budget=routed.budget)
            state = self._refresh_budget(state, started)
            state = self._transition(state, AgentStatus.PLANNING)
            self._runs.save_state(run_id, state)
            rejected = BudgetManager.check_planner(state.budget, phase="planner.before")
            if rejected is not None:
                raise BudgetExceeded(rejected)

            planned = await self._planner.plan(
                run_id=run_id, state=state, validator=PlanValidator(registry),
            )
            state = self._refresh_budget(planned.state, started)
            self._runs.save_state(run_id, state)
            rejected = BudgetManager.check_tool(state.budget, first_attempt=True)
            if rejected is not None:
                raise BudgetExceeded(rejected)

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
            if execution.budget_stop is not None:
                return self._budget_partial(run_id, state, execution.budget_stop)
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
            rejected = BudgetManager.check_model(state.budget, phase="diagnosis.before")
            if rejected is not None:
                raise BudgetExceeded(rejected)

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
            if exc.budget is not None:
                state = self._refresh_budget(_with_state(state, budget=exc.budget), started)
                self._runs.save_state(run_id, state)
            if isinstance(exc.error, BudgetRejection):
                return self._budget_partial(run_id, state, exc.error.reason)
            return self._fail(run_id, state, exc.error)
        except BudgetExceeded as exc:
            state = self._refresh_budget(_with_state(state, budget=exc.budget), started)
            self._runs.save_state(run_id, state)
            return self._budget_partial(run_id, state, exc.rejection.reason)
        except ModelGatewayError as exc:
            if exc.budget is not None:
                state = self._refresh_budget(_with_state(state, budget=exc.budget), started)
                self._runs.save_state(run_id, state)
            if exc.budget_stop is not None:
                return self._budget_partial(run_id, state, exc.budget_stop)
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
        self, run_id: str, state: AgentState, reason: BudgetStopReason,
    ) -> DiagnosisRunResult:
        try:
            state = persist_budget_partial(
                run_id=run_id, result_id=self._ids.new("result"), state=state,
                reason=reason, runs=self._runs, results=self._results,
                evidence_repository=self._tools, at=self._clock(),
            )
        except RuntimePersistenceError:
            return self._fail(run_id, state, ErrorInfo.from_code(
                ErrorCode.EXTERNAL_SERVICE_ERROR, "budget audit could not be persisted",
                retryable=False,
            ))
        return self._result(run_id, state, error=ErrorInfo.from_code(
            ErrorCode.BUDGET_EXCEEDED, "diagnosis stopped at the configured budget",
            retryable=False,
        ))

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
        budget = BudgetManager.refresh_elapsed(state.budget, self._monotonic() - started)
        return _with_state(state, budget=budget)


def _with_state(state: AgentState, **changes: object) -> AgentState:
    return AgentState.model_validate({**state.model_dump(mode="python"), **changes})


def _error_status(code: ErrorCode) -> AgentStatus:
    if code is ErrorCode.BUDGET_EXCEEDED:
        return AgentStatus.BUDGET_EXCEEDED
    if code in {ErrorCode.POLICY_REJECTED, ErrorCode.TOOL_NOT_FOUND}:
        return AgentStatus.POLICY_REJECTED
    return AgentStatus.FAILED
