from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opspilot.storage import (
    AgentRunRecord,
    Base,
    DiagnosisResultRecord,
    DiagnosisTaskRecord,
    EvidenceMutationError,
    EvidenceRecord,
    LLMCallRecord,
    PromptVersionRecord,
    ToolCallRecord,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        Base.metadata.create_all(connection)

    with Session(engine) as database_session:
        yield database_session

    engine.dispose()


def seed_complete_trace(session: Session) -> None:
    observed_at = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    task = DiagnosisTaskRecord(
        id="task_001",
        user_query="Why does payment-service keep restarting?",
        namespace="prod",
        status="COMPLETED",
    )
    run = AgentRunRecord(
        id="run_001",
        task_id=task.id,
        trace_id="trace_001",
        attempt_no=1,
        status="COMPLETED",
        state_payload={"status": "COMPLETED"},
        runtime_version="v0.1.0",
    )
    prompt = PromptVersionRecord(
        id="prompt_router_v1",
        component="router",
        version="v1",
        content_hash="a" * 64,
        template_text="Classify the diagnosis request.",
    )
    llm_call = LLMCallRecord(
        id="llm_001",
        run_id=run.id,
        prompt_version_id=prompt.id,
        sequence_no=1,
        component="router",
        provider="openai",
        model_name="test-model",
        model_version="2026-09-22",
        request_payload={"query": task.user_query},
        response_payload={"problem_type": "pod_restart"},
        input_tokens=100,
        output_tokens=20,
        cost_usd=Decimal("0.001"),
        latency_ms=120,
        retry_count=0,
        success=True,
    )
    tool_call = ToolCallRecord(
        id="call_001",
        run_id=run.id,
        sequence_no=1,
        tool_name="k8s.get_pod_events",
        tool_version="v1",
        risk_level=0,
        arguments_payload={"namespace": "prod", "pod": "payment-abc"},
        result_payload={"reason": "Unhealthy"},
        duration_ms=25,
        success=True,
    )
    evidence = EvidenceRecord(
        id="ev_001",
        run_id=run.id,
        tool_call_id=tool_call.id,
        source="kubernetes_events",
        resource="prod/payment-abc",
        observed_at=observed_at,
        collected_at=observed_at + timedelta(seconds=1),
        content="Liveness probe failed",
        source_confidence=Decimal("1.0"),
        raw_result_ref="result_001",
        attributes_payload=[{"key": "reason", "value": "Unhealthy"}],
    )
    result = DiagnosisResultRecord(
        id="result_001",
        task_id=task.id,
        run_id=run.id,
        status="COMPLETED",
        root_cause="Liveness Probe starts too early",
        recommendation="Add a startupProbe or increase the initial delay.",
        confidence=Decimal("0.91"),
        claims_payload=[
            {
                "claim_id": "claim_001",
                "evidence_ids": [evidence.id],
            }
        ],
        verification_payload={"supported": True},
    )

    session.add_all([task, run, prompt, llm_call, tool_call, evidence, result])
    session.commit()


def test_metadata_contains_exact_core_tables() -> None:
    assert set(Base.metadata.tables) == {
        "diagnosis_tasks",
        "agent_runs",
        "llm_calls",
        "tool_calls",
        "evidence",
        "diagnosis_results",
        "prompt_versions",
    }


def test_trace_links_calls_evidence_and_result(session: Session) -> None:
    seed_complete_trace(session)

    run = session.scalar(
        select(AgentRunRecord).where(AgentRunRecord.trace_id == "trace_001")
    )

    assert run is not None
    assert [call.id for call in run.llm_calls] == ["llm_001"]
    assert [call.id for call in run.tool_calls] == ["call_001"]
    assert [item.id for item in run.evidence] == ["ev_001"]
    assert run.result is not None
    assert run.result.id == "result_001"
    assert run.result.task.id == "task_001"


def test_evidence_is_append_only_through_orm(session: Session) -> None:
    seed_complete_trace(session)
    evidence = session.get_one(EvidenceRecord, "ev_001")

    evidence.content = "Changed content"
    with pytest.raises(EvidenceMutationError, match="append-only"):
        session.flush()
    session.rollback()

    evidence = session.get_one(EvidenceRecord, "ev_001")
    session.delete(evidence)
    with pytest.raises(EvidenceMutationError, match="cannot be deleted"):
        session.flush()


def test_evidence_tool_call_must_belong_to_same_run(session: Session) -> None:
    seed_complete_trace(session)
    second_run = AgentRunRecord(
        id="run_002",
        task_id="task_001",
        trace_id="trace_002",
        attempt_no=2,
        status="EXECUTING",
        state_payload={"status": "EXECUTING"},
        runtime_version="v0.1.0",
    )
    mismatched_evidence = EvidenceRecord(
        id="ev_002",
        run_id=second_run.id,
        tool_call_id="call_001",
        source="kubernetes_events",
        resource="prod/payment-abc",
        observed_at=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
        collected_at=datetime(2026, 9, 22, 10, 0, 1, tzinfo=UTC),
        content="Mismatched trace",
        source_confidence=Decimal("1.0"),
    )
    session.add_all([second_run, mismatched_evidence])

    with pytest.raises(IntegrityError):
        session.commit()

