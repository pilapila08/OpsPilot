"""Model boundary adapters for the shared BudgetManager."""

from __future__ import annotations

import asyncio
from time import perf_counter

from opspilot.agent.schemas import BudgetState
from opspilot.budget import BudgetExceeded, BudgetManager, BudgetRejection
from opspilot.llm.client import StructuredModelClient
from opspilot.llm.errors import ModelBudgetError, ModelGatewayError
from opspilot.llm.models import (
    ModelUsage, OutputT, StructuredModelRequest, StructuredModelResult,
)


def model_budget_error(rejection: BudgetRejection) -> ModelBudgetError:
    return ModelBudgetError(
        rejection.message, budget=rejection.reason.budget, budget_stop=rejection.reason,
    )


def ensure_model_budget(
    budget: BudgetState, *, phase: str = "model.before", after: bool = False,
) -> None:
    rejection = BudgetManager.check_model(budget, phase=phase, after=after)
    if rejection is not None:
        raise model_budget_error(rejection)


def ensure_planner_budget(budget: BudgetState, *, phase: str = "planner.before") -> None:
    rejection = BudgetManager.check_planner(budget, phase=phase)
    if rejection is not None:
        raise model_budget_error(rejection)


def consume_usage(
    budget: BudgetState,
    *,
    usage: ModelUsage,
    elapsed_seconds: float,
) -> BudgetState:
    return BudgetManager.consume_model(
        budget, tokens=usage.total_tokens, cost=usage.cost_usd,
        elapsed_seconds=elapsed_seconds,
    )


def consume_retry(budget: BudgetState, *, phase: str = "model.retry") -> BudgetState:
    try:
        return BudgetManager.consume_retry(budget, phase=phase)
    except BudgetExceeded as exc:
        raise model_budget_error(exc.rejection) from None


async def complete_with_budget(
    client: StructuredModelClient, request: StructuredModelRequest,
    output_model: type[OutputT], budget: BudgetState, *, phase: str,
) -> tuple[StructuredModelResult[OutputT], BudgetState]:
    """Bound a call by wall time and retain usage even when generation fails.

    Callers audit the response before checking its updated budget, preserving
    every paid attempt including a success whose usage crosses the run limit.
    """
    ensure_model_budget(budget, phase=f"{phase}.before")
    started = perf_counter()
    try:
        async with asyncio.timeout(BudgetManager.remaining_seconds(budget)):
            result = await client.complete(request, output_model)
    except TimeoutError:
        elapsed = max(0.0, perf_counter() - started)
        updated = consume_usage(budget, usage=ModelUsage(), elapsed_seconds=elapsed)
        error = model_budget_error(BudgetManager.timeout(updated, phase=f"{phase}.call"))
        error.latency_ms = round(elapsed * 1_000)
        raise error from None
    except ModelGatewayError as exc:
        exc.budget = consume_usage(
            budget, usage=exc.usage or ModelUsage(),
            elapsed_seconds=max(0.0, perf_counter() - started),
        )
        raise
    updated = consume_usage(
        budget, usage=result.metadata.usage,
        elapsed_seconds=max(0.0, perf_counter() - started),
    )
    return result, updated
