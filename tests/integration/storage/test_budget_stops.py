"""First budget-stop facts and the zero-Evidence V3 result contract."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from opspilot.agent.schemas import BudgetState
from opspilot.agent.state import AgentState
from opspilot.budget import BudgetStopReason
from opspilot.evidence.models import MissingEvidence
from opspilot.storage import Base, InMemoryRuntimeRepository, SQLAlchemyRuntimeRepository
from opspilot.storage.contracts import (
    BudgetStopSnapshot,
    BudgetVerificationV3,
    ResultSnapshot,
    RunSnapshot,
    RuntimePersistenceError,
    TaskSnapshot,
)

_AT = datetime(2026, 9, 25, 9, tzinfo=UTC)


def _run(run_id: str, trace_id: str, task_id: str) -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id, task_id=task_id,
        state=AgentState(task_id=task_id, trace_id=trace_id, user_query="diagnose"),
    )


def _stop(run_id: str = "run_one", trace_id: str = "trace_one") -> BudgetStopSnapshot:
    budget = BudgetState(steps_used=2, tool_calls_used=3, retries_used=1,
                         tokens_used=200, cost_usd=Decimal("0.0100"), elapsed_seconds=5.5)
    return BudgetStopSnapshot(
        run_id=run_id, trace_id=trace_id, round_no=2, recorded_at=_AT,
        reason=BudgetStopReason(
            dimension="tool_calls", phase="planner.before", step_no=3,
            kind="projected", requested=Decimal(13), budget=budget,
        ),
    )


def _check_repository(repo: InMemoryRuntimeRepository | SQLAlchemyRuntimeRepository) -> None:
    repo.create_task(TaskSnapshot(task_id="task_one", user_query="diagnose"))
    repo.create_task(TaskSnapshot(task_id="task_two", user_query="diagnose"))
    repo.create_run(_run("run_one", "trace_one", "task_one"))
    repo.create_run(_run("run_two", "trace_two", "task_two"))
    stop = _stop()
    repo.append_budget_stop(stop)
    repo.append_budget_stop(stop)
    assert repo.budget_stops_for_trace("trace_one") == (stop,)
    assert repo.budget_stops_for_trace("trace_two") == ()
    assert repo.budget_stops_for_trace("missing") == ()
    with pytest.raises(RuntimePersistenceError):
        repo.append_budget_stop(stop.model_copy(update={"round_no": 3}))
    with pytest.raises(RuntimePersistenceError):
        repo.append_budget_stop(_stop(trace_id="trace_two"))
    with pytest.raises(RuntimePersistenceError):
        repo.append_budget_stop(_stop(run_id="run_missing"))
    repo.append_budget_stop(_stop("run_two", "trace_two"))
    assert len(repo.budget_stops_for_trace("trace_two")) == 1


def test_in_memory_budget_stops_are_first_fact_and_isolated() -> None:
    _check_repository(InMemoryRuntimeRepository())


def test_sql_budget_stops_are_first_fact_and_isolated() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _check_repository(SQLAlchemyRuntimeRepository(session))
    engine.dispose()


def _v3_result() -> ResultSnapshot:
    verification = BudgetVerificationV3(
        missing_evidence=(MissingEvidence(
            requirement="A first observation", reason="Budget stopped before Tool execution",
        ),),
        rationale="No Evidence was collected before the budget stop.",
    )
    return ResultSnapshot(
        result_id="result_budget", task_id="task_one", run_id="run_one",
        status="PARTIAL", root_cause="Insufficient evidence",
        recommendation="Increase the time budget and retry", confidence=Decimal("0"),
        claims_payload=(), verification_payload=verification.model_dump(mode="json"),
        schema_version=3,
    )


def test_v3_budget_result_accepts_zero_evidence_and_rejects_fake_claims() -> None:
    result = _v3_result()
    assert result.verification_payload["checked_evidence_ids"] == []
    with pytest.raises(ValidationError):
        ResultSnapshot.model_validate({
            **result.model_dump(mode="python"), "status": "COMPLETED",
        })
    with pytest.raises(ValidationError):
        ResultSnapshot.model_validate({
            **result.model_dump(mode="python"),
            "claims_payload": ({"claim_id": "claim_fake"},),
        })
    with pytest.raises(ValidationError):
        ResultSnapshot.model_validate({
            **result.model_dump(mode="python"),
            "verification_payload": {"supported": False},
        })
    with pytest.raises(ValidationError):
        ResultSnapshot.model_validate({
            **result.model_dump(mode="python"),
            "verification_payload": {
                **result.verification_payload,
                "contradictions": [{"evidence_ids": ["ev_fake"], "reason": "fake"}],
            },
        })


def test_v3_budget_result_sql_round_trip() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repo = SQLAlchemyRuntimeRepository(session)
        repo.create_task(TaskSnapshot(task_id="task_one", user_query="diagnose"))
        repo.create_run(_run("run_one", "trace_one", "task_one"))
        repo.append_result(_v3_result())
        assert repo.get_result("run_one") == _v3_result()
    engine.dispose()
