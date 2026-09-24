"""One V2 read-only round with shared run budget and atomic Tool/Evidence audit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from pydantic import Field

from opspilot.agent.schemas import BudgetState, StrictSchema
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.errors import ErrorCode, ErrorInfo, error_policy
from opspilot.evidence import EvidenceExtractionError, EvidenceExtractorRegistry
from opspilot.evidence.models import Evidence
from opspilot.evidence.v2 import V2EvidenceExtractorRegistry
from opspilot.execution.models import ToolAttempt
from opspilot.execution.repository import ExecutionPersistenceError, ExecutionRepository
from opspilot.planning.v2 import AdmittedDecisionV2
from opspilot.tools import ToolInvocation, ToolRegistry
from opspilot.tools.models import ToolMetadata, ToolResponse, ToolRiskLevel


class V2RoundExecution(StrictSchema):
    state: AgentState
    new_evidence_ids: tuple[str, ...] = Field(max_length=100)
    new_tool_attempt_ids: tuple[str, ...] = Field(max_length=100)
    error: ErrorInfo | None = None


class V2RoundExecutor:
    def __init__(
        self, *, registry: ToolRegistry, repository: ExecutionRepository,
        extractors: EvidenceExtractorRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        tool_attempt_id_factory: Callable[[], str] | None = None,
        evidence_id_factory: Callable[[], str] | None = None,
        on_enter_executing: Callable[[AgentState], None] | None = None,
    ) -> None:
        self._registry = registry
        self._repository = repository
        self._extractors = extractors or V2EvidenceExtractorRegistry()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock
        self._tool_attempt_id = tool_attempt_id_factory or (lambda: f"tool_{uuid4().hex}")
        self._evidence_id = evidence_id_factory or (lambda: f"ev_{uuid4().hex}")
        self._on_enter = on_enter_executing

    async def execute(
        self, *, run_id: str, state: AgentState,
        admitted: AdmittedDecisionV2,
    ) -> V2RoundExecution:
        decision = admitted.decision
        if state.status is not AgentStatus.PLANNING or decision.action != "continue":
            raise ValueError("V2 executor requires an admitted continue round")
        if len(admitted.tool_versions) != len(decision.calls):
            raise ValueError("V2 admitted Tool versions are incomplete")
        executing = state.transition_to(
            AgentStatus.EXECUTING, at=max(self._clock(), state.updated_at)
        )
        if self._on_enter is not None:
            self._on_enter(executing)
        start = self._monotonic()
        base_elapsed = executing.budget.elapsed_seconds
        budget = executing.budget
        new_evidence: list[str] = []
        new_attempts: list[str] = []

        def finish(error: ErrorInfo | None = None) -> V2RoundExecution:
            progressed = AgentState.model_validate({
                **executing.model_dump(mode="python"),
                "budget": budget,
                "tool_call_ids": (*executing.tool_call_ids, *new_attempts),
                "evidence_ids": (*executing.evidence_ids, *new_evidence),
            })
            return V2RoundExecution(
                state=progressed, new_evidence_ids=tuple(new_evidence),
                new_tool_attempt_ids=tuple(new_attempts), error=error,
            )

        for call, (_, admitted_version) in zip(
            decision.calls, admitted.tool_versions, strict=True
        ):
            budget = _elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start))
            if _exhausted(budget):
                return finish(_error(ErrorCode.BUDGET_EXCEEDED, "V2 execution budget exhausted"))
            try:
                definition = self._registry.get(call.tool)
            except LookupError:
                return finish(_error(ErrorCode.TOOL_NOT_FOUND, "admitted Tool is unavailable"))
            if definition.version != admitted_version or definition.risk_level is not ToolRiskLevel.READ_ONLY:
                return finish(_error(ErrorCode.POLICY_REJECTED, "admitted Tool policy changed"))
            invocation = ToolInvocation(
                call_id=call.call_id, tool=call.tool, arguments=call.arguments
            )
            attempt_no = 0
            while True:
                budget = _elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start))
                if _attempt_exhausted(budget):
                    return finish(_error(ErrorCode.BUDGET_EXCEEDED, "V2 Tool budget exhausted"))
                attempt_no += 1
                started_at = self._clock()
                try:
                    async with asyncio.timeout(
                        budget.limits.timeout_seconds - budget.elapsed_seconds
                    ):
                        response = await self._registry.invoke(invocation)
                except TimeoutError:
                    response = _failed(
                        invocation, admitted_version, ErrorCode.BUDGET_EXCEEDED,
                        "V2 execution timed out",
                    )
                completed_at = max(self._clock(), started_at)
                budget = _elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start))
                budget = BudgetState.model_validate({
                    **budget.model_dump(mode="python"),
                    "steps_used": budget.steps_used + (1 if attempt_no == 1 else 0),
                    "tool_calls_used": budget.tool_calls_used + 1,
                })
                if (
                    response.metadata.call_id != invocation.call_id
                    or response.metadata.tool_name != invocation.tool
                    or response.metadata.tool_version != admitted_version
                ):
                    response = _failed(
                        invocation, admitted_version, ErrorCode.TOOL_OUTPUT_INVALID,
                        "Tool response identity is invalid",
                    )
                record_id = self._tool_attempt_id()
                evidence: tuple[Evidence, ...] = ()
                if response.success:
                    try:
                        evidence = self._extractors.extract(
                            invocation=invocation, response=response,
                            trace_id=state.trace_id, tool_attempt_id=record_id,
                            collected_at=completed_at,
                            evidence_id_factory=self._evidence_id,
                        )
                    except EvidenceExtractionError:
                        response = _failed(
                            invocation, admitted_version, ErrorCode.TOOL_OUTPUT_INVALID,
                            "Tool data could not be safely extracted",
                        )
                    if len(executing.evidence_ids) + len(new_evidence) + len(evidence) > 100:
                        evidence = ()
                        response = _failed(
                            invocation, admitted_version, ErrorCode.TOOL_OUTPUT_INVALID,
                            "Tool Evidence exceeds V2 observation limit",
                        )
                attempt = ToolAttempt(
                    record_id=record_id, run_id=run_id, trace_id=state.trace_id,
                    invocation=invocation, attempt_no=attempt_no,
                    risk_level=definition.risk_level, started_at=started_at,
                    completed_at=completed_at, response=response,
                )
                try:
                    self._repository.append_attempt(attempt, evidence)
                except ExecutionPersistenceError:
                    return finish(_error(
                        ErrorCode.EXTERNAL_SERVICE_ERROR, "Tool audit could not be persisted"
                    ))
                new_attempts.append(record_id)
                new_evidence.extend(item.evidence_id for item in evidence)
                if response.success:
                    break
                assert response.error is not None
                error = response.error
                retry_allowed = (
                    error.retryable and error_policy(error.code).retryable
                    and definition.retry_policy.permits(error.code)
                    and attempt_no <= definition.retry_policy.max_retries
                    and attempt_no <= error_policy(error.code).max_retries
                    and budget.retries_used < budget.limits.max_retries
                    and budget.tool_calls_used < budget.limits.max_tool_calls
                    and budget.elapsed_seconds < budget.limits.timeout_seconds
                )
                if not retry_allowed:
                    return finish(error)
                budget = BudgetState.model_validate({
                    **budget.model_dump(mode="python"),
                    "retries_used": budget.retries_used + 1,
                })
                backoff = min(
                    definition.retry_policy.initial_backoff_seconds
                    * definition.retry_policy.backoff_multiplier ** (attempt_no - 1),
                    definition.retry_policy.max_backoff_seconds,
                )
                try:
                    async with asyncio.timeout(
                        budget.limits.timeout_seconds - budget.elapsed_seconds
                    ):
                        await asyncio.sleep(backoff)
                except TimeoutError:
                    return finish(_error(
                        ErrorCode.BUDGET_EXCEEDED, "V2 Tool retry exceeded time budget"
                    ))
        budget = _elapsed(budget, base_elapsed + max(0.0, self._monotonic() - start))
        return finish()


def _elapsed(budget: BudgetState, elapsed: float) -> BudgetState:
    return BudgetState.model_validate({
        **budget.model_dump(mode="python"),
        "elapsed_seconds": max(budget.elapsed_seconds, elapsed),
    })


def _exhausted(budget: BudgetState) -> bool:
    return bool(set(budget.exhausted_dimensions) & {"steps", "tool_calls", "time"})


def _attempt_exhausted(budget: BudgetState) -> bool:
    return bool(set(budget.exhausted_dimensions) & {"tool_calls", "time"})


def _error(code: ErrorCode, message: str) -> ErrorInfo:
    return ErrorInfo.from_code(code, message, retryable=False)


def _failed(
    invocation: ToolInvocation, version: str, code: ErrorCode, message: str,
) -> ToolResponse:
    return ToolResponse(
        success=False, data=None,
        metadata=ToolMetadata(
            call_id=invocation.call_id, tool_name=invocation.tool,
            source="registry", duration_ms=0, tool_version=version,
        ),
        error=_error(code, message),
    )
