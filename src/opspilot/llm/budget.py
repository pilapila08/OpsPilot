"""Budget accounting shared by structured model callers."""

from __future__ import annotations

from opspilot.agent.schemas import BudgetState
from opspilot.llm.errors import ModelBudgetError
from opspilot.llm.models import ModelUsage


def ensure_model_budget(budget: BudgetState) -> None:
    if set(budget.exhausted_dimensions) & {"tokens", "cost", "time"}:
        raise ModelBudgetError("model call budget is exhausted")


def consume_usage(
    budget: BudgetState,
    *,
    usage: ModelUsage,
    latency_ms: int,
) -> BudgetState:
    return BudgetState(
        limits=budget.limits,
        steps_used=budget.steps_used,
        tool_calls_used=budget.tool_calls_used,
        retries_used=budget.retries_used,
        tokens_used=budget.tokens_used + usage.total_tokens,
        cost_usd=budget.cost_usd + usage.cost_usd,
        elapsed_seconds=budget.elapsed_seconds + latency_ms / 1_000,
    )


def consume_retry(budget: BudgetState) -> BudgetState:
    if budget.retries_used >= budget.limits.max_retries:
        raise ModelBudgetError("model regeneration retry budget is exhausted")
    return BudgetState(
        limits=budget.limits,
        steps_used=budget.steps_used,
        tool_calls_used=budget.tool_calls_used,
        retries_used=budget.retries_used + 1,
        tokens_used=budget.tokens_used,
        cost_usd=budget.cost_usd,
        elapsed_seconds=budget.elapsed_seconds,
    )
