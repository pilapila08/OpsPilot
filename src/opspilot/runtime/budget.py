"""Persist a budget stop and a claim-free partial report without more inference."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from opspilot.agent.state import AgentState, AgentStatus
from opspilot.budget import BudgetStopReason
from opspilot.evidence.models import MissingEvidence
from opspilot.storage.contracts import (
    BudgetStopSnapshot, BudgetVerificationV3, EvidenceRepository,
    ResultRepository, ResultSnapshot, RunRepository, RuntimePersistenceError,
)


def persist_budget_partial(
    *, run_id: str, result_id: str, state: AgentState,
    reason: BudgetStopReason, runs: RunRepository,
    results: ResultRepository, evidence_repository: EvidenceRepository,
    at: datetime, round_no: int | None = None,
) -> AgentState:
    """Budget termination is a runtime fact, never an unsupported root-cause claim."""
    evidence = evidence_repository.evidence_for_trace(state.trace_id)
    if (
        any(item.trace_id != state.trace_id for item in evidence)
        or {item.evidence_id for item in evidence} != set(state.evidence_ids)
    ):
        raise RuntimePersistenceError("budget partial Evidence differs from Run")
    recorded_at = max(at, state.updated_at)
    runs.append_budget_stop(BudgetStopSnapshot(
        run_id=run_id, trace_id=state.trace_id, reason=reason,
        round_no=round_no, recorded_at=recorded_at,
    ))
    verification = BudgetVerificationV3(
        checked_evidence_ids=tuple(item.evidence_id for item in evidence),
        missing_evidence=(MissingEvidence(
            requirement="completed_diagnosis",
            reason=f"The {reason.dimension} budget stopped work during {reason.phase}.",
        ),),
        rationale="The runtime retained observations but did not complete diagnosis; no root-cause claim is made.",
    )
    results.append_result(ResultSnapshot(
        result_id=result_id, task_id=state.task_id, run_id=run_id,
        status="PARTIAL", schema_version=3,
        root_cause="Diagnosis is incomplete because the configured budget stopped execution.",
        recommendation="Review the recorded budget stop and retained Evidence before starting a new run with appropriate limits.",
        confidence=Decimal("0"), claims_payload=(),
        verification_payload=verification.model_dump(mode="json"),
    ))
    updated = AgentState.model_validate({
        **state.model_dump(mode="python"),
        "diagnosis_id": result_id,
        "verification_id": f"verification_{result_id}",
    })
    if updated.status is not AgentStatus.BUDGET_EXCEEDED:
        updated = updated.transition_to(AgentStatus.BUDGET_EXCEEDED, at=recorded_at)
    runs.save_state(run_id, updated)
    return updated
