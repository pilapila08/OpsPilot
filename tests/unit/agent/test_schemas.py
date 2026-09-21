from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from opspilot.agent.schemas import (
    MAX_PLAN_STEPS,
    BudgetLimits,
    BudgetState,
    IntentOutput,
    Plan,
    PlanStep,
    Target,
)


def make_step(step_id: int) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool="k8s.get_pod_status",
        reason=f"Collect evidence for step {step_id}",
    )


def test_intent_round_trip_json() -> None:
    intent = IntentOutput(
        intent="diagnose",
        domain="kubernetes",
        problem_type="pod_restart",
        target=Target(namespace="prod", resource="payment-service"),
    )

    restored = IntentOutput.model_validate_json(intent.model_dump_json())

    assert restored == intent


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent", "repair"),
        ("domain", "database"),
        ("problem_type", "Pod Restart"),
    ],
)
def test_intent_rejects_invalid_classification(field: str, value: str) -> None:
    payload = {
        "intent": "diagnose",
        "domain": "kubernetes",
        "problem_type": "pod_restart",
        "target": {"namespace": "prod", "resource": "payment-service"},
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        IntentOutput.model_validate(payload)


@pytest.mark.parametrize(
    ("namespace", "resource"),
    [
        ("Prod", "payment-service"),
        ("prod", "../../etc/passwd"),
        ("prod_team", "payment-service"),
        ("prod", "payment_service"),
        ("prod", "payment..service"),
        ("prod", "payment.-service"),
    ],
)
def test_target_rejects_invalid_kubernetes_names(
    namespace: str,
    resource: str,
) -> None:
    with pytest.raises(ValidationError):
        Target(namespace=namespace, resource=resource)


def test_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Target.model_validate(
            {"namespace": "prod", "resource": "payment", "cluster": "main"}
        )


def test_schema_does_not_coerce_incorrect_types() -> None:
    with pytest.raises(ValidationError):
        PlanStep.model_validate(
            {
                "step_id": "1",
                "tool": "k8s.get_pod_status",
                "reason": "Collect pod status",
            }
        )


@pytest.mark.parametrize("model", [IntentOutput, Plan])
def test_structured_outputs_publish_closed_json_schema(
    model: type[BaseModel],
) -> None:
    schema = model.model_json_schema()

    assert schema["additionalProperties"] is False


def test_plan_accepts_maximum_number_of_sequential_steps() -> None:
    plan = Plan(steps=tuple(make_step(index) for index in range(1, 9)))

    assert len(plan.steps) == MAX_PLAN_STEPS


def test_plan_rejects_more_than_maximum_steps() -> None:
    with pytest.raises(ValidationError):
        Plan(steps=tuple(make_step(index) for index in range(1, 10)))


@pytest.mark.parametrize("step_ids", [(1, 1), (2,), (1, 3)])
def test_plan_rejects_duplicate_or_nonsequential_steps(
    step_ids: tuple[int, ...],
) -> None:
    with pytest.raises(ValidationError):
        Plan(steps=tuple(make_step(step_id) for step_id in step_ids))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_steps", 0),
        ("max_tool_calls", 0),
        ("max_retries", -1),
        ("max_tokens", 0),
        ("max_cost_usd", Decimal("0")),
        ("timeout_seconds", 0),
    ],
)
def test_budget_rejects_invalid_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        BudgetLimits.model_validate({field: value})


def test_budget_reports_all_exhausted_dimensions() -> None:
    budget = BudgetState(
        limits=BudgetLimits(
            max_steps=1,
            max_tool_calls=1,
            max_retries=1,
            max_tokens=100,
            max_cost_usd=Decimal("0.10"),
            timeout_seconds=10,
        ),
        steps_used=1,
        tool_calls_used=1,
        retries_used=1,
        tokens_used=100,
        cost_usd=Decimal("0.10"),
        elapsed_seconds=10,
    )

    assert budget.exhausted_dimensions == (
        "steps",
        "tool_calls",
        "retries",
        "tokens",
        "cost",
        "time",
    )
