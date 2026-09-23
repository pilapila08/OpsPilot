import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from opspilot.agent import BudgetState, ExecutableStepV1, ExecutionPlanV1, IntentOutput, Target
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.execution import BoundedExecutor
from opspilot.evidence import Evidence
from opspilot.execution.models import ToolAttempt
from opspilot.execution.repository import ExecutionPersistenceError
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.planning import PlanValidator, ValidatedPlanV1
from opspilot.storage import (
    AgentRunRecord,
    Base,
    DiagnosisTaskRecord,
    SQLAlchemyExecutionRepository,
)
from opspilot.tools.kubernetes import build_kubernetes_registry
from opspilot.tools import ToolInvocation, ToolMetadata, ToolResponse, ToolRiskLevel
from tests.integration.tools.test_kubernetes_registry import CaseReader


def _plan() -> ExecutionPlanV1:
    pod: dict[str, JsonValue] = {"namespace": "opspilot-fixtures", "workload_name": "slow-start-api"}
    return ExecutionPlanV1(
        schema_version=1,
        steps=(
            ExecutableStepV1(step_id=1, call_id="call_status", tool="k8s.get_pod_status", arguments=pod, reason="Read status"),
            ExecutableStepV1(step_id=2, call_id="call_events", tool="k8s.get_pod_events", arguments=pod, reason="Read events"),
            ExecutableStepV1(step_id=3, call_id="call_previous", tool="k8s.get_previous_logs", arguments=pod, reason="Read previous logs"),
            ExecutableStepV1(step_id=4, call_id="call_deployment", tool="k8s.get_deployment", arguments={"namespace": "opspilot-fixtures", "deployment_name": "slow-start-api"}, reason="Read probes"),
        ),
    )


def test_v0_case_through_real_handlers_produces_persisted_v1_evidence(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'execution.db').as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    at = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
    with Session(engine) as session:
        task = DiagnosisTaskRecord(
            id="task_001", user_query="Why is slow-start-api restarting?",
            namespace="opspilot-fixtures", status="PLANNING",
        )
        run = AgentRunRecord(
            id="run_001", task_id="task_001", trace_id="trace_001", attempt_no=1,
            status="PLANNING", state_payload={"status": "PLANNING"}, runtime_version="v1",
        )
        session.add_all([task, run])
        session.commit()
        registry = build_kubernetes_registry(CaseReader())
        intent = IntentOutput(
            intent="diagnose", domain="kubernetes", problem_type="pod_restart",
            target=Target(namespace="opspilot-fixtures", resource="slow-start-api"),
        )
        validation = PlanValidator(registry).validate(_plan(), intent=intent, budget=BudgetState())
        assert isinstance(validation, ValidatedPlanV1)
        state = AgentState(
            task_id="task_001", trace_id="trace_001", user_query=task.user_query,
            intent=intent, execution_plan_v1=validation.plan, status=AgentStatus.PLANNING,
            created_at=at, updated_at=at,
        )
        attempt_ids = iter(f"tool_{index:03d}" for index in range(1, 10))
        evidence_ids = iter(f"ev_{index:03d}" for index in range(1, 20))
        repository = SQLAlchemyExecutionRepository(session)
        executor = BoundedExecutor(
            registry=registry, repository=repository, clock=lambda: at,
            tool_attempt_id_factory=lambda: next(attempt_ids),
            evidence_id_factory=lambda: next(evidence_ids),
        )
        summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=validation))

        assert summary.error is None
        assert summary.state.status is AgentStatus.VERIFYING
        assert summary.completed_steps == 4
        assert summary.state.budget.steps_used == 4
        assert summary.state.budget.tool_calls_used == 4
        calls = repository.attempts_for_trace("trace_001")
        evidence = repository.evidence_for_trace("trace_001")
        assert [item.sequence_no for item in calls] == [1, 2, 3, 4]
        assert [item.logical_call_id for item in calls] == [
            "call_status", "call_events", "call_previous", "call_deployment"
        ]
        assert all(item.attempt_no == 1 and item.success for item in calls)
        assert len(evidence) >= 4
        assert all(item.tool_call_id in summary.tool_attempt_ids for item in evidence)
        assert {item.source for item in evidence} == {
            "kubernetes_status", "kubernetes_events", "kubernetes_logs", "kubernetes_deployment"
        }
        attributes = [item.attributes for item in evidence]
        keys = {entry.key for group in attributes for entry in group}
        assert {"restart_count", "liveness_failure", "startup_duration_seconds", "initial_delay_seconds"}.issubset(keys)
        assert all(item.raw_result_ref == item.tool_call_id for item in evidence)
    engine.dispose()


def test_failed_evidence_insert_rolls_back_only_its_tool_attempt() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    at = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([
            DiagnosisTaskRecord(id="task_001", user_query="diagnose api", namespace="team-a", status="EXECUTING"),
            AgentRunRecord(id="run_001", task_id="task_001", trace_id="trace_001", attempt_no=1, status="EXECUTING", state_payload={}, runtime_version="v1"),
        ])
        session.commit()
        repository = SQLAlchemyExecutionRepository(session)
        invocation = ToolInvocation(
            call_id="call_001", tool="k8s.get_pod_status",
            arguments={"namespace": "team-a", "workload_name": "api"},
        )
        response = ToolResponse(
            success=True, data={"namespace": "team-a"}, error=None,
            metadata=ToolMetadata(
                call_id="call_001", tool_name="k8s.get_pod_status", source="kubernetes",
                duration_ms=1, tool_version="v1",
            ),
        )

        def attempt(record_id: str, attempt_no: int) -> ToolAttempt:
            return ToolAttempt(
                record_id=record_id, run_id="run_001", trace_id="trace_001",
                invocation=invocation, attempt_no=attempt_no,
                risk_level=ToolRiskLevel.READ_ONLY, started_at=at,
                completed_at=at, response=response,
            )

        def evidence(record_id: str) -> Evidence:
            return Evidence(
                evidence_id="ev_001", trace_id="trace_001", tool_call_id=record_id,
                source="kubernetes_status", resource="team-a/api", observed_at=at,
                collected_at=at, content="Restart count is 5", source_confidence=1.0,
                raw_result_ref=record_id,
            )

        repository.append_attempt(attempt("tool_001", 1), (evidence("tool_001"),))
        with pytest.raises(ExecutionPersistenceError):
            repository.append_attempt(attempt("tool_002", 2), (evidence("tool_002"),))
        calls = repository.attempts_for_trace("trace_001")
        saved_evidence = repository.evidence_for_trace("trace_001")
        assert [item.record_id for item in calls] == ["tool_001"]
        assert [item.tool_call_id for item in saved_evidence] == ["tool_001"]
    engine.dispose()


def test_sql_trace_query_keeps_failed_retry_and_successful_attempt() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    at = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([
            DiagnosisTaskRecord(id="task_001", user_query="diagnose api", namespace="team-a", status="EXECUTING"),
            AgentRunRecord(id="run_001", task_id="task_001", trace_id="trace_001", attempt_no=1, status="EXECUTING", state_payload={}, runtime_version="v1"),
        ])
        session.commit()
        repository = SQLAlchemyExecutionRepository(session)
        invocation = ToolInvocation(
            call_id="call_001", tool="k8s.get_pod_status",
            arguments={"namespace": "team-a", "workload_name": "api"},
        )
        metadata = ToolMetadata(
            call_id="call_001", tool_name="k8s.get_pod_status", source="kubernetes",
            duration_ms=1, tool_version="v1",
        )
        failed = ToolAttempt(
            record_id="tool_001", run_id="run_001", trace_id="trace_001",
            invocation=invocation, attempt_no=1, risk_level=ToolRiskLevel.READ_ONLY,
            started_at=at, completed_at=at,
            response=ToolResponse(
                success=False, data=None, metadata=metadata,
                error=ErrorInfo.from_code(ErrorCode.TOOL_TIMEOUT, "timed out"),
            ),
        )
        succeeded = ToolAttempt(
            record_id="tool_002", run_id="run_001", trace_id="trace_001",
            invocation=invocation, attempt_no=2, risk_level=ToolRiskLevel.READ_ONLY,
            started_at=at, completed_at=at,
            response=ToolResponse(
                success=True, data={"namespace": "team-a"}, metadata=metadata, error=None,
            ),
        )
        evidence = Evidence(
            evidence_id="ev_001", trace_id="trace_001", tool_call_id="tool_002",
            source="kubernetes_status", resource="team-a/api", observed_at=at,
            collected_at=at, content="Restart count is 5", source_confidence=1.0,
            raw_result_ref="tool_002",
        )

        repository.append_attempt(failed, ())
        repository.append_attempt(succeeded, (evidence,))

        calls = repository.attempts_for_trace("trace_001")
        assert [(item.sequence_no, item.logical_call_id, item.attempt_no, item.success, item.error_code) for item in calls] == [
            (1, "call_001", 1, False, ErrorCode.TOOL_TIMEOUT),
            (2, "call_001", 2, True, None),
        ]
        assert [item.tool_call_id for item in repository.evidence_for_trace("trace_001")] == ["tool_002"]
    engine.dispose()
