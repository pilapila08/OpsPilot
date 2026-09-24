"""Observation-driven V2 runtime with explicit rounds and current-Trace verification."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from opspilot.agent.schemas import BudgetLimits, BudgetState, StrictSchema
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.budget import BudgetExceeded, BudgetManager, BudgetRejection, BudgetStopReason
from opspilot.diagnosis.v2 import V2Verifier
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.execution.repository import ExecutionRepository
from opspilot.execution.v2 import V2RoundExecutor
from opspilot.integrations.kubernetes.models import NamespaceName
from opspilot.llm.errors import ModelGatewayError
from opspilot.planning.v2 import ObservationSummaryV2, V2PlanValidator
from opspilot.planning.v2_planner import V2Planner, V2PlanningError
from opspilot.routing.v2 import IntentV2, V2IntentRouter
from opspilot.runtime.budget import persist_budget_partial
from opspilot.storage.contracts import (
    ResultRepository, ResultSnapshot, RunRepository, RunSnapshot,
    RuntimePersistenceError, TaskRepository, TaskSnapshot,
)
from opspilot.storage.planning_rounds import (
    PlanningRoundRepository, PlanningRoundV2, RoundPersistenceError,
)
from opspilot.tools import ToolRegistry


class V2DiagnosisRequest(StrictSchema):
    query: str = Field(min_length=1, max_length=4_000)
    namespace: NamespaceName
    mode: Literal["live", "replay"]
    case_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    branch_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{2,127}$")

    @model_validator(mode="after")
    def check_mode(self) -> V2DiagnosisRequest:
        if self.mode == "replay" and (self.case_id is None or self.branch_id is None):
            raise ValueError("V2 replay requires explicit case and branch")
        if self.mode == "live" and (self.case_id is not None or self.branch_id is not None):
            raise ValueError("V2 live cannot select a replay branch")
        return self


class V2RunResult(StrictSchema):
    task_id: str
    run_id: str
    trace_id: str
    status: AgentStatus
    state: AgentState
    diagnosis: ResultSnapshot | None
    error: ErrorInfo | None

    @model_validator(mode="after")
    def check_terminal(self) -> V2RunResult:
        if not self.status.is_terminal or self.state.status is not self.status:
            raise ValueError("V2 runtime result requires terminal state")
        if self.status in {AgentStatus.COMPLETED, AgentStatus.PARTIAL} and self.diagnosis is None:
            raise ValueError("V2 completed or partial state requires Result")
        if self.diagnosis is not None and self.diagnosis.schema_version not in {2, 3}:
            raise ValueError("V2 runtime requires versioned Result")
        return self


class ObservationRuntimeV2:
    def __init__(
        self, *, router: V2IntentRouter, planner: V2Planner, verifier: V2Verifier,
        task_repository: TaskRepository, run_repository: RunRepository,
        tool_repository: ExecutionRepository,
        round_repository: PlanningRoundRepository,
        result_repository: ResultRepository,
        registry_factory: Callable[[V2DiagnosisRequest], ToolRegistry],
        allowed_resources: Callable[[V2DiagnosisRequest], frozenset[str]],
        budget_limits: BudgetLimits | None = None,
        id_factory: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._router = router
        self._planner = planner
        self._verifier = verifier
        self._tasks = task_repository
        self._runs = run_repository
        self._tools = tool_repository
        self._rounds = round_repository
        self._results = result_repository
        self._registry_factory = registry_factory
        self._allowed_resources = allowed_resources
        self._limits = budget_limits or BudgetLimits()
        self._id = id_factory or (lambda kind: f"{kind}_{uuid4().hex}")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock

    async def run(self, request: V2DiagnosisRequest) -> V2RunResult:
        started = self._monotonic()
        task_id = self._id("task")
        run_id = self._id("run")
        trace_id = self._id("trace")
        now = self._clock()
        state = AgentState(
            task_id=task_id, trace_id=trace_id, user_query=request.query,
            budget=BudgetState(limits=self._limits), created_at=now, updated_at=now,
        )
        self._tasks.create_task(TaskSnapshot(
            task_id=task_id, user_query=request.query,
            namespace=request.namespace, mode=request.mode, case_id=request.case_id,
        ))
        self._runs.create_run(RunSnapshot(
            run_id=run_id, task_id=task_id, state=state, runtime_version="v2",
        ))
        active_round: int | None = None
        try:
            state = self._transition(state, AgentStatus.ROUTING)
            state = self._with_budget(state, state.budget, started)
            self._runs.save_state(run_id, state)
            rejected = BudgetManager.check_model(state.budget, phase="router.before")
            if rejected is not None:
                raise BudgetExceeded(rejected)
            routed = await self._router.route(
                run_id=run_id, query=request.query, namespace=request.namespace,
                budget=state.budget,
            )
            state = self._with_budget(state, routed.budget, started)
            self._runs.save_state(run_id, state)
            if routed.intent is None:
                return self._fail(
                    run_id, state,
                    ErrorInfo.from_code(ErrorCode.INVALID_ARGUMENT, "diagnosis target requires clarification"),
                )
            intent = routed.intent
            resources = self._allowed_resources(request)
            if intent.target.resource not in resources:
                return self._fail(
                    run_id, state,
                    ErrorInfo.from_code(ErrorCode.POLICY_REJECTED, "routed target is outside scope"),
                )
            registry = self._registry_factory(request)
            validator = V2PlanValidator(registry)
            state = self._transition(state, AgentStatus.PLANNING)
            self._runs.save_state(run_id, state)
            used_call_ids: set[str] = set()
            used_requests: set[tuple[str, str]] = set()
            observation_signatures: set[str] = set()

            for round_no in range(1, 5):
                active_round = round_no
                state = self._with_budget(state, state.budget, started)
                self._runs.save_state(run_id, state)
                rejected = BudgetManager.check_planner(state.budget, phase="planner.before")
                if rejected is not None:
                    return self._budget_partial(run_id, state, rejected.reason, round_no)
                evidence = self._tools.evidence_for_trace(trace_id)
                observation = ObservationSummaryV2.from_persisted(
                    trace_id, evidence,
                    tuple(
                        item.error_code for item in self._tools.attempts_for_trace(trace_id)
                        if item.error_code is not None
                    ),
                )
                try:
                    outcome = await self._planner.plan(
                        run_id=run_id, intent=intent, observation=observation,
                        round_no=round_no, budget=state.budget,
                        validator=validator,
                        used_call_ids=frozenset(used_call_ids),
                        used_requests=frozenset(used_requests),
                        allowed_resources=resources,
                    )
                except V2PlanningError as exc:
                    state = self._with_budget(state, exc.budget, started)
                    self._runs.save_state(run_id, state)
                    if isinstance(exc.error, BudgetRejection):
                        return self._budget_partial(run_id, state, exc.error.reason, round_no)
                    return self._fail(run_id, state, exc.error)
                state = self._with_budget(state, outcome.budget, started)
                self._runs.save_state(run_id, state)
                decision = outcome.admitted.decision
                renewed = validator.validate(
                    decision, intent=intent, round_no=round_no,
                    evidence_ids=observation.evidence_ids,
                    used_call_ids=frozenset(used_call_ids),
                    used_requests=frozenset(used_requests),
                    allowed_resources=resources, budget=state.budget,
                )
                if isinstance(renewed, BudgetRejection):
                    return self._budget_partial(run_id, state, renewed.reason, round_no)
                if isinstance(renewed, ErrorInfo) or renewed != outcome.admitted:
                    return self._fail(
                        run_id, state,
                        ErrorInfo.from_code(ErrorCode.POLICY_REJECTED, "V2 decision changed after admission"),
                    )
                self._rounds.append_round(PlanningRoundV2.from_decision(
                    run_id=run_id, prompt_version_id=outcome.prompt_version_id,
                    evidence_ids=observation.evidence_ids, decision=decision,
                ))
                if decision.action != "continue":
                    if not evidence:
                        return self._fail(
                            run_id, state,
                            ErrorInfo.from_code(ErrorCode.SCHEMA_VALIDATION, "no verifiable Evidence"),
                        )
                    return self._verify(
                        run_id, state, intent, force_partial=decision.action == "partial",
                    )
                for call in decision.calls:
                    definition = registry.get(call.tool)
                    normalized = definition.input_model.model_validate(
                        call.arguments, strict=True
                    ).model_dump(mode="json")
                    used_call_ids.add(call.call_id)
                    used_requests.add((
                        call.tool, json.dumps(normalized, sort_keys=True, separators=(",", ":"))
                    ))
                execution = await V2RoundExecutor(
                    registry=registry, repository=self._tools,
                    clock=self._clock, monotonic_clock=self._monotonic,
                    tool_attempt_id_factory=lambda: self._id("tool"),
                    evidence_id_factory=lambda: self._id("ev"),
                    on_enter_executing=lambda executing: self._runs.save_state(run_id, executing),
                ).execute(run_id=run_id, state=state, admitted=outcome.admitted)
                state = self._with_budget(execution.state, execution.state.budget, started)
                self._runs.save_state(run_id, state)
                if execution.budget_stop is not None:
                    return self._budget_partial(run_id, state, execution.budget_stop, round_no)
                if execution.error is not None:
                    code = execution.error.code
                    if code in {ErrorCode.POLICY_REJECTED, ErrorCode.TOOL_NOT_FOUND}:
                        return self._fail(run_id, state, execution.error)
                    if state.evidence_ids:
                        return self._verify(
                            run_id, state, intent, force_partial=True,
                            terminal=AgentStatus.PARTIAL,
                            error=execution.error,
                        )
                    return self._fail(run_id, state, execution.error)
                if not execution.new_evidence_ids:
                    if state.evidence_ids:
                        return self._verify(run_id, state, intent, force_partial=True)
                    return self._fail(
                        run_id, state,
                        ErrorInfo.from_code(ErrorCode.SCHEMA_VALIDATION, "no verifiable Evidence"),
                    )
                next_observation = ObservationSummaryV2.from_persisted(
                    trace_id, self._tools.evidence_for_trace(trace_id)
                )
                signature = _observation_signature(next_observation)
                if signature in observation_signatures or round_no == 4:
                    return self._verify(run_id, state, intent, force_partial=True)
                observation_signatures.add(signature)
                state = self._transition(state, AgentStatus.PLANNING)
                self._runs.save_state(run_id, state)
            raise AssertionError("bounded V2 round loop did not terminate")
        except BudgetExceeded as exc:
            state = self._with_budget(state, exc.budget, started)
            self._runs.save_state(run_id, state)
            return self._budget_partial(run_id, state, exc.rejection.reason, active_round)
        except ModelGatewayError as exc:
            if exc.budget is not None:
                state = self._with_budget(state, exc.budget, started)
                self._runs.save_state(run_id, state)
            if exc.budget_stop is not None:
                return self._budget_partial(run_id, state, exc.budget_stop, active_round)
            return self._fail(run_id, state, exc.to_error_info(retryable=False))
        except (RuntimePersistenceError, RoundPersistenceError):
            return self._fail(run_id, state, ErrorInfo.from_code(
                ErrorCode.EXTERNAL_SERVICE_ERROR, "V2 audit could not be persisted"
            ))
        except Exception:
            return self._fail(run_id, state, ErrorInfo.from_code(
                ErrorCode.EXTERNAL_SERVICE_ERROR, "V2 runtime stage failed"
            ))

    def _verify(
        self, run_id: str, state: AgentState, intent: IntentV2, *,
        force_partial: bool, terminal: AgentStatus | None = None,
        error: ErrorInfo | None = None,
    ) -> V2RunResult:
        evidence = self._tools.evidence_for_trace(state.trace_id)
        if not evidence:
            return self._fail(run_id, state, ErrorInfo.from_code(
                ErrorCode.SCHEMA_VALIDATION, "no verifiable Evidence"
            ))
        verifying = self._transition(state, AgentStatus.VERIFYING)
        self._runs.save_state(run_id, verifying)
        assessment = self._verifier.verify(
            intent=intent, evidence=evidence, force_partial=force_partial
        )
        current_ids = {item.evidence_id for item in evidence}
        if (
            not set(assessment.claim.evidence_ids).issubset(current_ids)
            or not set(assessment.verification.checked_evidence_ids).issubset(current_ids)
            or (force_partial and assessment.status != "PARTIAL")
        ):
            return self._fail(run_id, verifying, ErrorInfo.from_code(
                ErrorCode.POLICY_REJECTED, "V2 Verifier cited foreign Evidence or upgraded Partial"
            ))
        result_id = self._id("result")
        self._results.append_result(ResultSnapshot(
            result_id=result_id, task_id=state.task_id, run_id=run_id,
            status=assessment.status, root_cause=assessment.root_cause,
            recommendation=assessment.recommendation, confidence=assessment.confidence,
            claims_payload=(assessment.claim.model_dump(mode="json"),),
            verification_payload=assessment.verification.model_dump(mode="json"),
            schema_version=2,
        ))
        final_status = terminal or (
            AgentStatus.COMPLETED if assessment.status == "COMPLETED" else AgentStatus.PARTIAL
        )
        verifying = AgentState.model_validate({
            **verifying.model_dump(mode="python"),
            "diagnosis_id": result_id,
            "verification_id": f"verification_{result_id}",
        })
        final = self._transition(verifying, final_status)
        self._runs.save_state(run_id, final)
        return self._result(run_id, final, error)

    def _fail(self, run_id: str, state: AgentState, error: ErrorInfo) -> V2RunResult:
        latest = self._runs.get_run(run_id)
        if latest is not None:
            state = latest.state
        if not state.status.is_terminal:
            target = (
                AgentStatus.BUDGET_EXCEEDED if error.code is ErrorCode.BUDGET_EXCEEDED
                else AgentStatus.POLICY_REJECTED if error.code in {
                    ErrorCode.POLICY_REJECTED, ErrorCode.TOOL_NOT_FOUND
                } else AgentStatus.FAILED
            )
            state = self._transition(state, target)
            self._runs.save_state(run_id, state)
        return self._result(run_id, state, error)

    def _budget_partial(
        self, run_id: str, state: AgentState, reason: BudgetStopReason,
        round_no: int | None,
    ) -> V2RunResult:
        try:
            state = persist_budget_partial(
                run_id=run_id, result_id=self._id("result"), state=state,
                reason=reason, runs=self._runs, results=self._results,
                evidence_repository=self._tools, at=self._clock(), round_no=round_no,
            )
        except RuntimePersistenceError:
            return self._fail(run_id, state, ErrorInfo.from_code(
                ErrorCode.EXTERNAL_SERVICE_ERROR, "budget audit could not be persisted",
                retryable=False,
            ))
        return self._result(run_id, state, ErrorInfo.from_code(
            ErrorCode.BUDGET_EXCEEDED, "diagnosis stopped at the configured budget",
            retryable=False,
        ))

    def _result(self, run_id: str, state: AgentState, error: ErrorInfo | None) -> V2RunResult:
        return V2RunResult(
            task_id=state.task_id, run_id=run_id, trace_id=state.trace_id,
            status=state.status, state=state,
            diagnosis=self._results.get_result(run_id), error=error,
        )

    def _transition(self, state: AgentState, target: AgentStatus) -> AgentState:
        return state.transition_to(target, at=max(self._clock(), state.updated_at))

    def _with_budget(self, state: AgentState, budget: BudgetState, started: float) -> AgentState:
        updated = BudgetManager.refresh_elapsed(budget, self._monotonic() - started)
        return AgentState.model_validate({
            **state.model_dump(mode="python"), "budget": updated,
        })


def _observation_signature(summary: ObservationSummaryV2) -> str:
    payload = sorted({
        (item.source, item.resource, item.keys, item.categories,
         tuple((fact.key, fact.value) for fact in item.facts))
        for item in summary.signals
    })
    return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
