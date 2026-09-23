from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from opspilot.agent.state import AgentState, AgentStatus
from opspilot.execution.repository import ExecutionRepository
from opspilot.llm.audit import InMemoryModelAuditRepository
from opspilot.storage import (
    Base,
    InMemoryRuntimeRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyModelAuditRepository,
    SQLAlchemyRuntimeRepository,
)
from opspilot.storage.contracts import (
    EvidenceRepository,
    LlmCallRepository,
    ResultRepository,
    ResultSnapshot,
    RunRepository,
    RunSnapshot,
    TaskRepository,
    TaskSnapshot,
    ToolCallRepository,
)


def _state() -> AgentState:
    return AgentState(
        task_id="task_001", trace_id="trace_001",
        user_query="Why is api restarting?",
    )


def _result() -> ResultSnapshot:
    return ResultSnapshot(
        result_id="result_001", task_id="task_001", run_id="run_001",
        status="PARTIAL", root_cause="Insufficient evidence",
        recommendation="Collect previous logs", confidence=Decimal("0.25"),
        claims_payload=(), verification_payload={"supported": False},
    )


def _exercise_runtime_repository(
    repository: SQLAlchemyRuntimeRepository | InMemoryRuntimeRepository,
) -> None:
    assert isinstance(repository, TaskRepository)
    assert isinstance(repository, RunRepository)
    assert isinstance(repository, ResultRepository)
    repository.create_task(TaskSnapshot(
        task_id="task_001", user_query="Why is api restarting?", namespace="team-a"
    ))
    repository.create_run(RunSnapshot(run_id="run_001", task_id="task_001", state=_state()))
    updated = _state().transition_to(AgentStatus.ROUTING)
    repository.save_state("run_001", updated)
    task = repository.get_task("task_001")
    run = repository.get_run("run_001")
    assert task is not None and task.status is AgentStatus.ROUTING
    assert run is not None and run.state.status is AgentStatus.ROUTING
    repository.append_result(_result())
    assert repository.get_result("run_001") == _result()


def test_in_memory_runtime_repository_round_trip() -> None:
    _exercise_runtime_repository(InMemoryRuntimeRepository())
    assert isinstance(InMemoryModelAuditRepository(), LlmCallRepository)


def test_sqlalchemy_runtime_repository_round_trip_and_protocols() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _exercise_runtime_repository(SQLAlchemyRuntimeRepository(session))
        execution = SQLAlchemyExecutionRepository(session)
        assert isinstance(execution, ExecutionRepository)
        assert isinstance(execution, ToolCallRepository)
        assert isinstance(execution, EvidenceRepository)
        assert isinstance(SQLAlchemyModelAuditRepository(session), LlmCallRepository)
    engine.dispose()
