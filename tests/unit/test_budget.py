import asyncio
from decimal import Decimal

import pytest

from opspilot.agent.schemas import BudgetLimits, BudgetState, StrictSchema
from opspilot.budget import BudgetExceeded, BudgetManager, BudgetRejection
from opspilot.errors import ErrorAction, ErrorCode
from opspilot.llm.budget import complete_with_budget
from opspilot.llm.errors import ModelBudgetError
from opspilot.llm.models import (
    ModelMessage, ModelRole, OutputT, PromptReference,
    StructuredModelConfig, StructuredModelRequest, StructuredModelResult,
)


@pytest.mark.parametrize(
    ("budget", "dimension"),
    [
        (BudgetState(steps_used=8), "steps"),
        (BudgetState(tool_calls_used=15), "tool_calls"),
        (BudgetState(tokens_used=30_000), "tokens"),
        (BudgetState(cost_usd=Decimal("0.15")), "cost"),
        (BudgetState(elapsed_seconds=90), "elapsed"),
    ],
)
def test_planner_stops_before_a_model_call_with_exact_dimension(
    budget: BudgetState, dimension: str,
) -> None:
    rejection = BudgetManager.check_planner(budget)
    assert isinstance(rejection, BudgetRejection)
    assert rejection.code is ErrorCode.BUDGET_EXCEEDED
    assert rejection.action is ErrorAction.RETURN_PARTIAL
    assert rejection.reason.dimension == dimension
    assert rejection.reason.step_no == budget.steps_used + 1
    assert rejection.reason.budget == budget


def test_zero_retry_quota_allows_first_calls_and_stops_only_retry() -> None:
    budget = BudgetState(limits=BudgetLimits(max_retries=0))
    assert BudgetManager.check_model(budget) is None
    assert BudgetManager.check_planner(budget) is None
    assert BudgetManager.check_tool(budget, first_attempt=True) is None
    with pytest.raises(BudgetExceeded) as caught:
        BudgetManager.consume_retry(budget)
    assert caught.value.rejection.reason.dimension == "retries"
    assert caught.value.budget == budget


def test_retry_of_last_logical_step_uses_call_and_retry_quota() -> None:
    budget = BudgetState(limits=BudgetLimits(max_steps=1), steps_used=1, tool_calls_used=1)
    assert BudgetManager.check_tool(budget, first_attempt=False) is None
    assert BudgetManager.check_retry(budget, tool=True) is None
    budget = BudgetManager.consume_retry(budget)
    budget = BudgetManager.consume_tool(budget, first_attempt=False)
    assert (budget.steps_used, budget.tool_calls_used, budget.retries_used) == (1, 2, 1)


def test_tool_stops_distinguish_current_attempt_from_next_logical_step() -> None:
    budget = BudgetState(tool_calls_used=15, steps_used=2, retries_used=2)
    first = BudgetManager.check_tool(budget, first_attempt=True)
    retry = BudgetManager.check_tool(budget, first_attempt=False)
    quota = BudgetManager.check_retry(budget, tool=True, phase="tool.retry")
    deadline = BudgetManager.timeout(budget, phase="tool.call")
    assert first is not None and first.reason.step_no == 3
    assert retry is not None and retry.reason.step_no == 2
    assert quota is not None and quota.reason.step_no == 2
    assert deadline.reason.step_no == 2
    expired = BudgetManager.timeout(budget, phase="tool.backoff").reason.budget
    after = BudgetManager.check_model(expired, phase="tool.after", after=True)
    assert after is not None and after.reason.step_no == 2
    planner = BudgetManager.check_planner(budget)
    assert planner is not None and planner.reason.step_no == 3


@pytest.mark.parametrize(
    ("budget", "dimension", "requested"),
    [
        (BudgetState(limits=BudgetLimits(max_steps=1)), "steps", Decimal(2)),
        (BudgetState(limits=BudgetLimits(max_tool_calls=3)), "tool_calls", Decimal(4)),
        (BudgetState(limits=BudgetLimits(timeout_seconds=5)), "elapsed", Decimal(8)),
    ],
)
def test_plan_projection_records_unconsumed_work_and_reserves_worst_retries(
    budget: BudgetState, dimension: str, requested: Decimal,
) -> None:
    rejection = BudgetManager.admit_plan(
        budget, steps=2, timeouts=(1.0, 2.0), retry_timeouts=(1.0, 2.0, 3.0),
    )
    assert rejection is not None
    assert rejection.reason.dimension == dimension
    assert rejection.reason.kind == "projected"
    assert rejection.reason.requested == requested
    assert rejection.reason.budget.steps_used == 0


def test_micro_cost_is_preserved_and_post_call_limit_is_distinct_from_admission() -> None:
    budget = BudgetState(limits=BudgetLimits(max_tokens=1, max_cost_usd=Decimal("0.000001")))
    budget = BudgetManager.consume_model(
        budget, tokens=1, cost=Decimal("0.000001"), elapsed_seconds=0.002,
    )
    assert budget.cost_usd == Decimal("0.000001")
    assert BudgetManager.check_model(budget, after=True) is None
    assert BudgetManager.check_model(budget) is not None
    overshot = BudgetManager.consume_model(
        budget, tokens=0, cost=Decimal("0.000001"), elapsed_seconds=0,
    )
    rejection = BudgetManager.check_model(overshot, after=True)
    assert rejection is not None and rejection.reason.dimension == "cost"
    assert rejection.reason.budget.cost_usd == Decimal("0.000002")


class WaitingClient:
    cancelled = False

    async def complete(
        self, request: StructuredModelRequest, output_model: type[OutputT],
    ) -> StructuredModelResult[OutputT]:
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True
        raise AssertionError("unreachable")


def test_model_is_cancelled_at_remaining_run_deadline() -> None:
    request = StructuredModelRequest(
        messages=(ModelMessage(role=ModelRole.USER, content="classify"),),
        prompt=PromptReference(component="router", version="v1", content_hash="a" * 64),
        config=StructuredModelConfig(provider="test", model="test"),
    )
    client = WaitingClient()
    budget = BudgetState(limits=BudgetLimits(timeout_seconds=1), elapsed_seconds=0.99)
    with pytest.raises(ModelBudgetError) as caught:
        asyncio.run(complete_with_budget(client, request, StrictSchema, budget, phase="router"))
    assert client.cancelled
    assert caught.value.budget is not None
    assert caught.value.budget.elapsed_seconds >= 1
    assert caught.value.budget_stop is not None
    assert caught.value.budget_stop.dimension == "elapsed"
    assert caught.value.budget_stop.kind == "deadline"
    assert caught.value.budget_stop.phase == "router.call"
