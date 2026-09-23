import json
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from opspilot.agent import (
    BudgetLimits,
    BudgetState,
    ExecutableStepV1,
    ExecutionPlanV1,
    IntentOutput,
    Plan,
    PlanStep,
    Target,
)
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.integrations.kubernetes import KubernetesReader
from opspilot.planning import PlanValidator, ValidatedPlanV1
from opspilot.tools import ToolDefinition, ToolRegistry, ToolRiskLevel
from opspilot.tools.kubernetes import build_kubernetes_registry
from opspilot.tools.kubernetes.models import PodStatusInput, PodStatusOutput


def _intent() -> IntentOutput:
    return IntentOutput(
        intent="diagnose",
        domain="kubernetes",
        problem_type="pod_restart",
        target=Target(namespace="opspilot-fixtures", resource="slow-start-api"),
    )


def _registry() -> ToolRegistry:
    return build_kubernetes_registry(cast(KubernetesReader, object()))


def _step(index: int, tool: str, **arguments: object) -> ExecutableStepV1:
    return ExecutableStepV1(
        step_id=index,
        call_id=f"call_{index:03d}",
        tool=tool,
        arguments=cast(dict[str, JsonValue], arguments),
        reason="Collect one diagnostic fact",
    )


def _plan() -> ExecutionPlanV1:
    pod = {"namespace": "opspilot-fixtures", "workload_name": "slow-start-api"}
    return ExecutionPlanV1(
        schema_version=1,
        steps=(
            _step(1, "k8s.get_pod_status", **pod),
            _step(2, "k8s.get_pod_events", **pod),
            _step(3, "k8s.get_previous_logs", **pod),
            _step(
                4,
                "k8s.get_deployment",
                namespace="opspilot-fixtures",
                deployment_name="slow-start-api",
            ),
        ),
    )


def _replace_step(plan: ExecutionPlanV1, index: int, step: ExecutableStepV1) -> ExecutionPlanV1:
    steps = list(plan.steps)
    steps[index] = step
    return ExecutionPlanV1(schema_version=1, steps=tuple(steps))


def _reject(plan: ExecutionPlanV1, code: ErrorCode, *, budget: BudgetState | None = None) -> None:
    result = PlanValidator(_registry()).validate(
        plan, intent=_intent(), budget=budget or BudgetState()
    )
    assert isinstance(result, ErrorInfo)
    assert result.code is code


def test_case_plan_validates_against_all_five_descriptors_without_handler_calls() -> None:
    result = PlanValidator(_registry()).validate(_plan(), intent=_intent(), budget=BudgetState())

    assert isinstance(result, ValidatedPlanV1)
    assert [step.tool for step in result.plan.steps] == [
        "k8s.get_pod_status",
        "k8s.get_pod_events",
        "k8s.get_previous_logs",
        "k8s.get_deployment",
    ]
    assert len(result.tool_versions) == 4


def test_validated_plan_cannot_be_changed_through_nested_arguments() -> None:
    result = PlanValidator(_registry()).validate(_plan(), intent=_intent(), budget=BudgetState())
    assert isinstance(result, ValidatedPlanV1)
    mutable_copy = result.plan
    mutable_copy.steps[0].arguments["namespace"] = "prod"
    assert result.plan.steps[0].arguments["namespace"] == "opspilot-fixtures"


@pytest.mark.parametrize("risk", [ToolRiskLevel.APPROVAL_REQUIRED, ToolRiskLevel.PROHIBITED])
def test_unknown_tool_and_non_read_only_tool_are_rejected(risk: ToolRiskLevel) -> None:
    plan = _replace_step(
        _plan(), 0, _step(1, "k8s.delete_pod", namespace="opspilot-fixtures", workload_name="slow-start-api")
    )
    _reject(plan, ErrorCode.TOOL_NOT_FOUND)

    async def forbidden_handler(_: PodStatusInput) -> PodStatusOutput:
        raise AssertionError("validator must not execute handlers")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="k8s.get_pod_status",
            description="Policy controlled status read",
            risk_level=risk,
            input_model=PodStatusInput,
            output_model=PodStatusOutput,
            handler=forbidden_handler,
            source="test",
        )
    )
    result = PlanValidator(registry).validate(_plan(), intent=_intent(), budget=BudgetState())
    assert isinstance(result, ErrorInfo)
    assert result.code is ErrorCode.POLICY_REJECTED


def test_same_tool_name_with_expanded_input_model_is_rejected() -> None:
    class ExpandedStatusInput(PodStatusInput):
        shell: str

    async def forbidden_handler(_: ExpandedStatusInput) -> PodStatusOutput:
        raise AssertionError("validator must not execute handlers")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="k8s.get_pod_status",
            description="Misconfigured status read",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=ExpandedStatusInput,
            output_model=PodStatusOutput,
            handler=forbidden_handler,
            source="test",
        )
    )
    result = PlanValidator(registry).validate(_plan(), intent=_intent(), budget=BudgetState())
    assert isinstance(result, ErrorInfo)
    assert result.code is ErrorCode.POLICY_REJECTED


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"namespace": "prod", "workload_name": "slow-start-api"}, ErrorCode.POLICY_REJECTED),
        ({"namespace": "opspilot-fixtures", "workload_name": "other"}, ErrorCode.POLICY_REJECTED),
        ({"namespace": "opspilot-fixtures", "workload_name": "slow-start-api", "label_selector": "x=y"}, ErrorCode.INVALID_ARGUMENT),
        ({"namespace": "opspilot-fixtures", "workload_name": "slow-start-api", "shell": "whoami"}, ErrorCode.INVALID_ARGUMENT),
    ],
)
def test_scope_and_extra_arguments_are_rejected(arguments: dict[str, object], code: ErrorCode) -> None:
    plan = _replace_step(_plan(), 0, _step(1, "k8s.get_pod_status", **arguments))
    _reject(plan, code)


def test_strict_input_validation_rejects_type_coercion() -> None:
    plan = _replace_step(
        _plan(), 2,
        _step(3, "k8s.get_previous_logs", namespace="opspilot-fixtures", workload_name="slow-start-api", tail_lines="200"),
    )
    _reject(plan, ErrorCode.INVALID_ARGUMENT)


def test_duplicate_calls_are_detected_after_default_normalization() -> None:
    first = _step(1, "k8s.get_pod_status", namespace="opspilot-fixtures", workload_name="slow-start-api")
    duplicate = _step(2, "k8s.get_pod_status", namespace="opspilot-fixtures", workload_name="slow-start-api", pod_name=None)
    _reject(ExecutionPlanV1(schema_version=1, steps=(first, duplicate)), ErrorCode.INVALID_ARGUMENT)


@pytest.mark.parametrize(
    ("budget", "expected_message"),
    [
        (BudgetState(limits=BudgetLimits(max_steps=3)), "step"),
        (BudgetState(limits=BudgetLimits(max_tool_calls=5)), "tool call"),
        (BudgetState(limits=BudgetLimits(timeout_seconds=59)), "time"),
        (BudgetState(limits=BudgetLimits(max_tokens=10), tokens_used=10), "model"),
    ],
)
def test_budget_rejects_plan_before_execution(budget: BudgetState, expected_message: str) -> None:
    result = PlanValidator(_registry()).validate(_plan(), intent=_intent(), budget=budget)
    assert isinstance(result, ErrorInfo)
    assert result.code is ErrorCode.BUDGET_EXCEEDED
    assert expected_message in result.message


def test_zero_retry_budget_allows_read_only_plan_without_retry_reservation() -> None:
    budget = BudgetState(limits=BudgetLimits(max_retries=0, max_tool_calls=4, timeout_seconds=40))
    assert isinstance(
        PlanValidator(_registry()).validate(_plan(), intent=_intent(), budget=budget),
        ValidatedPlanV1,
    )


def test_descriptor_registration_order_does_not_change_validation() -> None:
    registry = _registry()
    reversed_registry = ToolRegistry()
    for descriptor in reversed(registry.descriptors()):
        reversed_registry.register(registry.get(descriptor.name))
    first = PlanValidator(registry).validate(_plan(), intent=_intent(), budget=BudgetState())
    second = PlanValidator(reversed_registry).validate(_plan(), intent=_intent(), budget=BudgetState())
    assert first == second


def test_v1_schema_rejects_nonsequential_duplicate_and_oversized_plans() -> None:
    base = _plan().model_dump(mode="json")
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate({**base, "steps": [{**base["steps"][0], "step_id": 2}]})
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate({**base, "steps": [base["steps"][0], {**base["steps"][1], "call_id": "call_001"}]})
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate({**base, "steps": [base["steps"][0]] * 9})


def test_v0_plan_and_state_deserialize_without_v1_fields() -> None:
    old_state = AgentState(
        task_id="task_001",
        trace_id="trace_001",
        user_query="Why is the service restarting?",
        plan=Plan(steps=(PlanStep(step_id=1, tool="k8s.get_pod_status", reason="Read status"),)),
        status=AgentStatus.PLANNING,
    )
    old_payload = old_state.model_dump(mode="json")
    del old_payload["execution_plan_v1"]
    restored = AgentState.model_validate_json(json.dumps(old_payload))
    assert restored.plan == old_state.plan
    assert restored.execution_plan_v1 is None
