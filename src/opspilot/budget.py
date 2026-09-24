"""One admission and accounting policy for every runtime budget dimension."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from opspilot.agent.schemas import BudgetState, StrictSchema
from opspilot.errors import ErrorAction, ErrorCategory, ErrorCode, ErrorInfo

BudgetDimension = Literal["steps", "tool_calls", "retries", "tokens", "cost", "elapsed"]


class BudgetStopReason(StrictSchema):
    """A stop at the pending step, or the current step for Tool retry/completion."""
    dimension: BudgetDimension
    phase: str = Field(min_length=1, max_length=128)
    step_no: int = Field(ge=1)
    kind: Literal["exhausted", "projected", "deadline"]
    requested: Decimal = Field(default=Decimal(0), ge=0)
    budget: BudgetState


class BudgetRejection(ErrorInfo):
    reason: BudgetStopReason


class BudgetExceeded(RuntimeError):
    def __init__(self, rejection: BudgetRejection) -> None:
        self.rejection = rejection
        self.budget = rejection.reason.budget
        super().__init__(rejection.message)


class BudgetManager:
    """Pure checks and immutable updates; the Runtime persists stop decisions.

    Tokens and cost are charged from actual provider usage. A response may cross
    those limits once because its usage is unknown before execution. It is then
    rejected immediately, and no subsequent model or Tool call is admitted.
    Elapsed time always comes from the local monotonic clock, never model metadata.
    """

    @staticmethod
    def _reject(
        budget: BudgetState, dimension: BudgetDimension, phase: str, *,
        kind: Literal["exhausted", "projected", "deadline"] = "exhausted",
        requested: Decimal = Decimal(0),
        step_no: int | None = None,
    ) -> BudgetRejection:
        label = {
            "steps": "step", "tool_calls": "tool call", "retries": "retry",
            "tokens": "model token", "cost": "model cost", "elapsed": "elapsed time",
        }[dimension]
        detail = "cannot admit projected work" if kind == "projected" else "is exhausted"
        if step_no is None:
            step_no = (
                max(1, budget.steps_used)
                if phase in {"tool.call", "tool.retry", "tool.backoff", "tool.after"}
                else budget.steps_used + 1
            )
        return BudgetRejection(
            code=ErrorCode.BUDGET_EXCEEDED, category=ErrorCategory.BUDGET,
            action=ErrorAction.RETURN_PARTIAL, retryable=False,
            message=f"{phase}: {label} budget {detail}",
            reason=BudgetStopReason(
                dimension=dimension, phase=phase, step_no=step_no,
                kind=kind, requested=requested, budget=budget,
            ),
        )

    @staticmethod
    def check_model(
        budget: BudgetState, *, phase: str = "model.before", after: bool = False,
    ) -> BudgetRejection | None:
        for dimension, used, limit in (
            ("tokens", Decimal(budget.tokens_used), Decimal(budget.limits.max_tokens)),
            ("cost", budget.cost_usd, budget.limits.max_cost_usd),
        ):
            if used > limit or (not after and used == limit):
                assert dimension in {"tokens", "cost"}
                return BudgetManager._reject(
                    budget, "tokens" if dimension == "tokens" else "cost", phase
                )
        if budget.elapsed_seconds >= budget.limits.timeout_seconds:
            return BudgetManager._reject(budget, "elapsed", phase)
        return None

    @staticmethod
    def check_planner(
        budget: BudgetState, *, phase: str = "planner.before",
    ) -> BudgetRejection | None:
        if budget.steps_used >= budget.limits.max_steps:
            return BudgetManager._reject(budget, "steps", phase)
        if budget.tool_calls_used >= budget.limits.max_tool_calls:
            return BudgetManager._reject(budget, "tool_calls", phase)
        return BudgetManager.check_model(budget, phase=phase)

    @staticmethod
    def check_tool(
        budget: BudgetState, *, first_attempt: bool, phase: str = "tool.before",
    ) -> BudgetRejection | None:
        step_no = budget.steps_used + 1 if first_attempt else max(1, budget.steps_used)
        if first_attempt and budget.steps_used >= budget.limits.max_steps:
            return BudgetManager._reject(budget, "steps", phase, step_no=step_no)
        if budget.tool_calls_used >= budget.limits.max_tool_calls:
            return BudgetManager._reject(budget, "tool_calls", phase, step_no=step_no)
        rejection = BudgetManager.check_model(budget, phase=phase)
        if rejection is not None:
            return rejection.model_copy(update={
                "reason": rejection.reason.model_copy(update={"step_no": step_no}),
            })
        return None

    @staticmethod
    def check_retry(
        budget: BudgetState, *, tool: bool = False, phase: str = "model.retry",
    ) -> BudgetRejection | None:
        if budget.retries_used >= budget.limits.max_retries:
            return BudgetManager._reject(
                budget, "retries", phase,
                step_no=max(1, budget.steps_used) if tool else None,
            )
        if tool:
            return BudgetManager.check_tool(budget, first_attempt=False, phase=phase)
        return BudgetManager.check_model(budget, phase=phase)

    @staticmethod
    def admit_plan(
        budget: BudgetState, *, steps: int, timeouts: tuple[float, ...] = (),
        retry_timeouts: tuple[float, ...] = (), phase: str = "plan.admission",
    ) -> BudgetRejection | None:
        if steps < 0 or any(item < 0 for item in (*timeouts, *retry_timeouts)):
            raise ValueError("budget projections cannot be negative")
        if budget.steps_used + steps > budget.limits.max_steps:
            return BudgetManager._reject(
                budget, "steps", phase, kind="projected", requested=Decimal(steps)
            )
        retries = min(
            len(retry_timeouts), max(0, budget.limits.max_retries - budget.retries_used)
        )
        calls = steps + retries
        if budget.tool_calls_used + calls > budget.limits.max_tool_calls:
            return BudgetManager._reject(
                budget, "tool_calls", phase, kind="projected", requested=Decimal(calls)
            )
        rejection = BudgetManager.check_model(budget, phase=phase)
        if rejection is not None:
            return rejection
        projected = sum(timeouts) + sum(sorted(retry_timeouts, reverse=True)[:retries])
        if budget.elapsed_seconds + projected > budget.limits.timeout_seconds:
            return BudgetManager._reject(
                budget, "elapsed", phase, kind="projected", requested=Decimal(str(projected))
            )
        return None

    @staticmethod
    def consume_tool(budget: BudgetState, *, first_attempt: bool) -> BudgetState:
        return BudgetState.model_validate({
            **budget.model_dump(mode="python"),
            "steps_used": budget.steps_used + int(first_attempt),
            "tool_calls_used": budget.tool_calls_used + 1,
        })

    @staticmethod
    def consume_retry(budget: BudgetState, *, phase: str = "model.retry") -> BudgetState:
        rejection = BudgetManager.check_retry(budget, phase=phase)
        if rejection is not None:
            raise BudgetExceeded(rejection)
        return BudgetState.model_validate({
            **budget.model_dump(mode="python"), "retries_used": budget.retries_used + 1,
        })

    @staticmethod
    def consume_model(
        budget: BudgetState, *, tokens: int, cost: Decimal, elapsed_seconds: float,
    ) -> BudgetState:
        if tokens < 0 or cost < 0 or elapsed_seconds < 0:
            raise ValueError("budget consumption cannot be negative")
        return BudgetState.model_validate({
            **budget.model_dump(mode="python"),
            "tokens_used": budget.tokens_used + tokens,
            "cost_usd": budget.cost_usd + cost,
            "elapsed_seconds": budget.elapsed_seconds + elapsed_seconds,
        })

    @staticmethod
    def refresh_elapsed(budget: BudgetState, elapsed: float) -> BudgetState:
        return BudgetState.model_validate({
            **budget.model_dump(mode="python"),
            "elapsed_seconds": max(budget.elapsed_seconds, elapsed),
        })

    @staticmethod
    def remaining_seconds(budget: BudgetState) -> float:
        return max(0.0, budget.limits.timeout_seconds - budget.elapsed_seconds)

    @staticmethod
    def timeout(budget: BudgetState, *, phase: str) -> BudgetRejection:
        expired = BudgetManager.refresh_elapsed(budget, float(budget.limits.timeout_seconds))
        return BudgetManager._reject(expired, "elapsed", phase, kind="deadline")
