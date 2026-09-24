import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import JsonValue

from opspilot.agent import BudgetLimits, BudgetState, ExecutableStepV1, ExecutionPlanV1, IntentOutput, Target
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.execution import BoundedExecutor, InMemoryExecutionRepository
from opspilot.planning import PlanValidator, ValidatedPlanV1
from opspilot.tools import ToolInvocation, ToolMetadata, ToolRegistry, ToolResponse
from opspilot.tools.kubernetes import build_kubernetes_registry
from opspilot.integrations.kubernetes import KubernetesReader

AT = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)


def _plan(*, two_steps: bool = False) -> ExecutionPlanV1:
    pod: dict[str, JsonValue] = {"namespace": "team-a", "workload_name": "api"}
    steps = [
        ExecutableStepV1(step_id=1, call_id="call_status", tool="k8s.get_pod_status", arguments=pod, reason="Read status")
    ]
    if two_steps:
        steps.append(
            ExecutableStepV1(step_id=2, call_id="call_events", tool="k8s.get_pod_events", arguments=pod, reason="Read events")
        )
    return ExecutionPlanV1(schema_version=1, steps=tuple(steps))


def _intent() -> IntentOutput:
    return IntentOutput(
        intent="diagnose", domain="kubernetes", problem_type="pod_restart",
        target=Target(namespace="team-a", resource="api"),
    )


def _response(invocation: ToolInvocation, *, code: ErrorCode | None = None) -> ToolResponse:
    metadata = ToolMetadata(
        call_id=invocation.call_id, tool_name=invocation.tool, source="kubernetes",
        duration_ms=5, tool_version="v1",
    )
    if code is not None:
        return ToolResponse(
            success=False, data=None, metadata=metadata,
            error=ErrorInfo.from_code(code, "stable fake error", retryable=code is ErrorCode.TOOL_TIMEOUT),
        )
    if invocation.tool == "k8s.get_pod_status":
        data: dict[str, JsonValue] = {
            "namespace": "team-a", "pod_name": "api-123", "phase": "Running",
            "conditions": [],
            "containers": [{"name": "api", "ready": False, "restart_count": 5, "state": "waiting", "reason": "CrashLoopBackOff"}],
        }
    else:
        data = {"namespace": "team-a", "pod_name": "api-123", "events": []}
    return ToolResponse(success=True, data=data, metadata=metadata, error=None)


class ScriptedRegistry(ToolRegistry):
    def __init__(self, codes: list[ErrorCode | None], *, ticks: list[float] | None = None) -> None:
        super().__init__()
        source = build_kubernetes_registry(cast(KubernetesReader, object()))
        for descriptor in source.descriptors():
            self.register(source.get(descriptor.name))
        self.codes = list(codes)
        self.invocations: list[ToolInvocation] = []
        self.ticks = ticks

    async def invoke(self, invocation: ToolInvocation) -> ToolResponse:
        self.invocations.append(invocation)
        if self.ticks is not None:
            self.ticks[0] += 95.0
        return _response(invocation, code=self.codes.pop(0))


def _setup(
    registry: ScriptedRegistry,
    *,
    two_steps: bool = False,
    budget: BudgetState | None = None,
    repository: InMemoryExecutionRepository | None = None,
    ticks: list[float] | None = None,
) -> tuple[BoundedExecutor, AgentState, ValidatedPlanV1, InMemoryExecutionRepository]:
    effective_budget = budget or BudgetState()
    plan = _plan(two_steps=two_steps)
    validation = PlanValidator(registry).validate(plan, intent=_intent(), budget=effective_budget)
    assert isinstance(validation, ValidatedPlanV1)
    state = AgentState(
        task_id="task_001", trace_id="trace_001", user_query="Why is api restarting?",
        intent=_intent(), execution_plan_v1=plan, status=AgentStatus.PLANNING,
        budget=effective_budget, created_at=AT, updated_at=AT,
    )
    sink = repository or InMemoryExecutionRepository()
    attempt_ids = iter(f"tool_{index:03d}" for index in range(1, 10))
    evidence_ids = iter(f"ev_{index:03d}" for index in range(1, 10))
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    executor = BoundedExecutor(
        registry=registry, repository=sink, clock=lambda: AT,
        monotonic_clock=(lambda: ticks[0]) if ticks is not None else (lambda: 0.0),
        sleep=fake_sleep,
        tool_attempt_id_factory=lambda: next(attempt_ids),
        evidence_id_factory=lambda: next(evidence_ids),
    )
    return executor, state, validation, sink


def test_timeout_retries_once_with_same_logical_call_and_second_attempt_evidence() -> None:
    registry = ScriptedRegistry([ErrorCode.TOOL_TIMEOUT, None])
    executor, state, plan, sink = _setup(registry)
    summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=plan))

    assert summary.error is None
    assert summary.state.status is AgentStatus.VERIFYING
    assert summary.state.budget.tool_calls_used == 2
    assert summary.state.budget.steps_used == 1
    assert summary.state.budget.retries_used == 1
    assert [item.attempt_no for item in sink.attempts] == [1, 2]
    assert [item.invocation.call_id for item in sink.attempts] == ["call_status", "call_status"]
    assert sink.attempts[0].response.error is not None
    assert sink.attempts[0].response.error.code is ErrorCode.TOOL_TIMEOUT
    assert sink.evidence[0].tool_call_id == "tool_002"
    assert len(registry.invocations) == 2


def test_permission_failure_is_not_retried_and_without_evidence_fails() -> None:
    registry = ScriptedRegistry([ErrorCode.PERMISSION_DENIED])
    executor, state, plan, sink = _setup(registry)
    summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=plan))
    assert summary.error is not None and summary.error.code is ErrorCode.PERMISSION_DENIED
    assert summary.state.status is AgentStatus.FAILED
    assert summary.state.budget.tool_calls_used == 1
    assert len(sink.attempts) == 1
    assert sink.evidence == []


def test_later_failure_preserves_prior_evidence_for_partial_decision() -> None:
    registry = ScriptedRegistry([None, ErrorCode.PERMISSION_DENIED])
    executor, state, plan, sink = _setup(registry, two_steps=True)
    summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=plan))
    assert summary.error is not None and summary.error.code is ErrorCode.PERMISSION_DENIED
    assert summary.state.status is AgentStatus.EXECUTING
    assert summary.completed_steps == 1
    assert summary.evidence_ids == ("ev_001",)
    assert len(sink.attempts) == 2
    assert len(sink.evidence) == 1


def test_retryable_failure_with_zero_retry_budget_stops_as_budget_exceeded() -> None:
    registry = ScriptedRegistry([ErrorCode.TOOL_TIMEOUT])
    budget = BudgetState(limits=BudgetLimits(max_retries=0, max_tool_calls=1, timeout_seconds=10))
    executor, state, plan, sink = _setup(registry, budget=budget)
    summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=plan))
    assert summary.error is not None and summary.error.code is ErrorCode.BUDGET_EXCEEDED
    assert summary.state.status is AgentStatus.BUDGET_EXCEEDED
    assert len(sink.attempts) == 1
    assert len(registry.invocations) == 1


def test_elapsed_budget_stops_before_next_tool_after_success() -> None:
    ticks = [0.0]
    registry = ScriptedRegistry([None], ticks=ticks)
    executor, state, plan, sink = _setup(registry, two_steps=True, ticks=ticks)
    summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=plan))
    assert summary.error is not None and summary.error.code is ErrorCode.BUDGET_EXCEEDED
    assert summary.state.status is AgentStatus.BUDGET_EXCEEDED
    assert summary.state.budget.tool_calls_used == 1
    assert len(registry.invocations) == 1
    assert len(sink.evidence) == 1


def test_unvalidated_plan_object_cannot_enter_executor() -> None:
    registry = ScriptedRegistry([None])
    executor, state, _, _ = _setup(registry)
    with pytest.raises(ValueError, match="validated V1 plan"):
        asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=cast(ValidatedPlanV1, _plan())))


def test_elapsed_stop_keeps_failed_tool_audit_without_retrying() -> None:
    ticks = [0.0]
    registry = ScriptedRegistry([ErrorCode.PERMISSION_DENIED], ticks=ticks)
    executor, state, plan, sink = _setup(registry, ticks=ticks)
    summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=plan))
    assert summary.state.status is AgentStatus.BUDGET_EXCEEDED
    assert summary.budget_stop is not None
    assert summary.budget_stop.dimension == "elapsed"
    assert summary.budget_stop.phase == "tool.after"
    assert summary.budget_stop.step_no == 1
    assert summary.budget_stop.budget.elapsed_seconds == 95
    assert len(sink.attempts) == 1
    assert sink.attempts[0].response.error is not None
    assert sink.attempts[0].response.error.code is ErrorCode.PERMISSION_DENIED
