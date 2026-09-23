import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from alembic import command
from alembic.config import Config
from pydantic import JsonValue
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from opspilot.agent import BudgetLimits, ExecutableStepV1, ExecutionPlanV1
from opspilot.agent.state import AgentStatus
from opspilot.cases import LoadedCase, load_case
from opspilot.diagnosis import DiagnosisDraftV1, V1DiagnosisAssembler
from opspilot.evidence import Claim
from opspilot.errors import ErrorCode
from opspilot.llm.client import ScriptedModelClient
from opspilot.llm.models import ScriptedModelResponse, StructuredModelConfig
from opspilot.llm.prompts import load_prompt
from opspilot.planning import V1Planner
from opspilot.routing import IntentRouter
from opspilot.runtime import (
    DiagnosisRequest, DiagnosisRuntime, ReplayRegistryFactory, ReplayToolRegistry,
)
from opspilot.storage.contracts import ResultSnapshot, RuntimePersistenceError
from opspilot.tools import ToolInvocation, ToolRegistry, ToolResponse
from opspilot.tools.kubernetes import build_kubernetes_registry
from tests.integration.tools.test_kubernetes_registry import CaseReader
from opspilot.storage import (
    AgentRunRecord, DiagnosisTaskRecord, EvidenceRecord, LLMCallRecord,
    PromptVersionRecord, SQLAlchemyExecutionRepository,
    SQLAlchemyModelAuditRepository, SQLAlchemyRuntimeRepository,
    ToolCallRecord,
)

ROOT = Path(__file__).resolve().parents[3]
CASE_FILE = ROOT / "fixtures/cases/crashloop-liveness-v1/case.json"
REQUEST = DiagnosisRequest(
    query="Why is slow-start-api restarting?", namespace="opspilot-fixtures",
    mode="replay", case_id="crashloop_liveness_v1",
)


class SequentialIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def new(self, kind: str) -> str:
        number = self.counts.get(kind, 0) + 1
        self.counts[kind] = number
        return f"{kind}_{number:03d}"


def _plan() -> ExecutionPlanV1:
    pod: dict[str, JsonValue] = {"namespace": "opspilot-fixtures", "workload_name": "slow-start-api"}
    return ExecutionPlanV1(
        schema_version=1,
        steps=(
            ExecutableStepV1(step_id=1, call_id="call_pod_status", tool="k8s.get_pod_status", arguments=pod, reason="Read Pod restarts"),
            ExecutableStepV1(step_id=2, call_id="call_pod_events", tool="k8s.get_pod_events", arguments=pod, reason="Read probe events"),
            ExecutableStepV1(step_id=3, call_id="call_previous_logs", tool="k8s.get_previous_logs", arguments={**pod, "container_name": "slow-start-api"}, reason="Read terminated container logs"),
            ExecutableStepV1(step_id=4, call_id="call_deployment", tool="k8s.get_deployment", arguments={"namespace": "opspilot-fixtures", "deployment_name": "slow-start-api"}, reason="Read probe configuration"),
        ),
    )


def _model_steps(*, first_evidence: int = 1, evidence_count: int = 5) -> list[ScriptedModelResponse]:
    case = load_case(CASE_FILE)
    expected = case.definition.expected_diagnosis
    draft = DiagnosisDraftV1(
        schema_version=1, root_cause=expected.root_cause,
        recommendation=expected.recommendation,
        claims=(Claim(
            claim_id=expected.claim.claim_id, text=expected.claim.text,
            evidence_ids=tuple(
                f"ev_{index:03d}"
                for index in range(first_evidence, first_evidence + evidence_count)
            ),
            inference_confidence=expected.claim.inference_confidence,
        ),),
    )
    return [
        ScriptedModelResponse(payload={
            "intent": "diagnose", "domain": "kubernetes",
            "problem_type": "pod_restart", "resource": "slow-start-api",
        }, input_tokens=40, output_tokens=10),
        ScriptedModelResponse(payload=_plan().model_dump(mode="json"), input_tokens=80, output_tokens=30),
        ScriptedModelResponse(payload=draft.model_dump(mode="json"), input_tokens=100, output_tokens=50),
    ]


def _database(tmp_path: Path) -> Engine:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'runtime.db').as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return create_engine(database_url)


def _runtime(
    session: Session,
    client: ScriptedModelClient,
    ids: SequentialIds,
    *,
    registry_factory: Callable[[DiagnosisRequest], ToolRegistry] | None = None,
    budget_limits: BudgetLimits | None = None,
    storage: SQLAlchemyRuntimeRepository | None = None,
) -> DiagnosisRuntime:
    storage = storage or SQLAlchemyRuntimeRepository(session)
    audit = SQLAlchemyModelAuditRepository(session)
    tools = SQLAlchemyExecutionRepository(session)
    config = StructuredModelConfig(provider="test", model="scripted")
    return DiagnosisRuntime(
        router=IntentRouter(
            client=client, audit_repository=audit,
            prompt=load_prompt(ROOT / "prompts/router/v1.md", component="router", version="v1"),
            model_config=config,
        ),
        planner=V1Planner(
            client=client, audit_repository=audit,
            prompt=load_prompt(ROOT / "prompts/planner/v1.md", component="planner", version="v1"),
            model_config=config,
        ),
        diagnosis=V1DiagnosisAssembler(
            client=client, audit_repository=audit,
            evidence_repository=tools, run_repository=storage,
            result_repository=storage,
            prompt=load_prompt(ROOT / "prompts/diagnosis/v1.md", component="diagnosis", version="v1"),
            model_config=config,
        ),
        task_repository=storage, run_repository=storage,
        tool_repository=tools, result_repository=storage,
        registry_factory=registry_factory or ReplayRegistryFactory({"crashloop_liveness_v1": CASE_FILE}),
        budget_limits=budget_limits, ids=ids,
    )


def test_offline_case_completes_all_stages_on_migrated_database(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    case = load_case(CASE_FILE)
    with Session(engine) as session:
        client = ScriptedModelClient(_model_steps())
        runtime = _runtime(session, client, SequentialIds())
        output = asyncio.run(runtime.run(REQUEST))

        assert output.status is AgentStatus.COMPLETED, output.error
        assert output.diagnosis is not None
        assert output.diagnosis.root_cause == case.definition.expected_diagnosis.root_cause
        assert output.diagnosis.recommendation == case.definition.expected_diagnosis.recommendation
        assert output.diagnosis.verification_payload["supported"] is True
        assert [item.to_status for item in output.state.transitions] == [
            AgentStatus.ROUTING, AgentStatus.PLANNING, AgentStatus.EXECUTING,
            AgentStatus.VERIFYING, AgentStatus.COMPLETED,
        ]
        assert output.state.budget.tool_calls_used == 4
        assert output.state.budget.tokens_used == 310
        assert client.call_count == 3

        task = session.get_one(DiagnosisTaskRecord, output.task_id)
        run = session.get_one(AgentRunRecord, output.run_id)
        assert task.status == run.status == "COMPLETED"
        assert run.trace_id == output.trace_id
        assert run.state_payload["status"] == "COMPLETED"
        llm_calls = session.scalars(select(LLMCallRecord).where(LLMCallRecord.run_id == output.run_id).order_by(LLMCallRecord.sequence_no)).all()
        tool_calls = session.scalars(select(ToolCallRecord).where(ToolCallRecord.run_id == output.run_id).order_by(ToolCallRecord.sequence_no)).all()
        evidence = session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == output.run_id)).all()
        assert [item.component for item in llm_calls] == ["router", "planner", "diagnosis"]
        assert [item.logical_call_id for item in tool_calls] == [
            "call_pod_status", "call_pod_events", "call_previous_logs", "call_deployment",
        ]
        assert all(item.tool_version == "v1" and item.success for item in tool_calls)
        assert {item.source for item in evidence} == {
            "kubernetes_status", "kubernetes_events", "kubernetes_logs", "kubernetes_deployment",
        }
        assert output.diagnosis.claims_payload[0]["evidence_ids"] == sorted(item.id for item in evidence)
        assert {item.component for item in session.scalars(select(PromptVersionRecord)).all()} == {
            "router", "planner", "diagnosis",
        }
    engine.dispose()


def test_missing_previous_logs_produces_persisted_partial_result(tmp_path: Path) -> None:
    engine = _database(tmp_path)

    def without_previous_logs(path: Path) -> LoadedCase:
        case = load_case(path)
        responses = tuple(
            response.model_copy(update={"data": {**response.data, "content": ""}})
            if response.metadata.tool_name == "k8s.get_previous_logs" and response.data is not None
            else response
            for response in case.responses
        )
        return replace(case, responses=responses)

    factory = ReplayRegistryFactory(
        {"crashloop_liveness_v1": CASE_FILE}, loader=without_previous_logs,
    )
    with Session(engine) as session:
        runtime = _runtime(
            session, ScriptedModelClient(_model_steps(evidence_count=4)),
            SequentialIds(), registry_factory=factory,
        )
        output = asyncio.run(runtime.run(REQUEST))
        assert output.status is AgentStatus.PARTIAL, output.error
        assert output.diagnosis is not None
        missing = output.diagnosis.verification_payload["missing_evidence"]
        assert isinstance(missing, list)
        assert any(isinstance(item, dict) and item.get("requirement") == "startup_duration" for item in missing)
        assert len(session.scalars(select(ToolCallRecord)).all()) == 4
        assert len(session.scalars(select(EvidenceRecord)).all()) == 4
    engine.dispose()


def test_router_schema_exhaustion_stops_before_planning_and_tools(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    client = ScriptedModelClient([ScriptedModelResponse(payload={}) for _ in range(3)])
    with Session(engine) as session:
        output = asyncio.run(_runtime(session, client, SequentialIds()).run(REQUEST))
        assert output.status is AgentStatus.FAILED
        assert output.error is not None and output.error.code is ErrorCode.SCHEMA_VALIDATION
        assert client.call_count == 3
        assert [item.component for item in session.scalars(select(LLMCallRecord)).all()] == ["router"] * 3
        assert session.scalars(select(ToolCallRecord)).all() == []
    engine.dispose()


def test_planner_unknown_tool_never_reaches_registry(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    steps = _model_steps()
    invalid = _plan().model_dump(mode="json")
    invalid["steps"][0]["tool"] = "k8s.unknown"
    client = ScriptedModelClient([steps[0], ScriptedModelResponse(payload=invalid), ScriptedModelResponse(payload=invalid)])
    with Session(engine) as session:
        output = asyncio.run(_runtime(session, client, SequentialIds()).run(REQUEST))
        assert output.status is AgentStatus.POLICY_REJECTED
        assert output.error is not None and output.error.code is ErrorCode.TOOL_NOT_FOUND
        assert client.call_count == 3
        assert session.scalars(select(ToolCallRecord)).all() == []
    engine.dispose()


class TimeoutEventsRegistry(ReplayToolRegistry):
    async def invoke(self, invocation: ToolInvocation) -> ToolResponse:
        if invocation.call_id == "call_pod_events":
            return self._failure(
                invocation=invocation, started_at=perf_counter(),
                code=ErrorCode.TOOL_TIMEOUT, message="replay event read timed out",
                definition=self.get(invocation.tool),
            )
        return await super().invoke(invocation)


def test_second_tool_timeout_exhausts_retry_budget_without_extra_calls(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    client = ScriptedModelClient(_model_steps()[:2])
    factory = lambda request: TimeoutEventsRegistry(load_case(CASE_FILE))
    with Session(engine) as session:
        output = asyncio.run(_runtime(
            session, client, SequentialIds(), registry_factory=factory,
            budget_limits=BudgetLimits(max_retries=0, max_tool_calls=4),
        ).run(REQUEST))
        assert output.status is AgentStatus.BUDGET_EXCEEDED
        assert output.error is not None and output.error.code is ErrorCode.BUDGET_EXCEEDED
        assert output.diagnosis is not None and output.diagnosis.status == "PARTIAL"
        assert output.diagnosis.verification_payload["supported"] is False
        assert client.call_count == 2
        assert output.state.budget.tool_calls_used == 2
        assert [item.success for item in session.scalars(select(ToolCallRecord).order_by(ToolCallRecord.sequence_no)).all()] == [True, False]
        assert len(session.scalars(select(EvidenceRecord)).all()) == 1
        assert [item.component for item in session.scalars(select(LLMCallRecord)).all()] == ["router", "planner"]
    engine.dispose()


class FailingResultRepository(SQLAlchemyRuntimeRepository):
    def append_result(self, result: ResultSnapshot) -> None:
        del result
        raise RuntimePersistenceError("simulated Result write failure")


def test_result_write_failure_marks_run_failed_and_keeps_evidence(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    with Session(engine) as session:
        runtime = _runtime(
            session, ScriptedModelClient(_model_steps()), SequentialIds(),
            storage=FailingResultRepository(session),
        )
        output = asyncio.run(runtime.run(REQUEST))
        assert output.status is AgentStatus.FAILED
        assert output.error is not None and output.error.code is ErrorCode.EXTERNAL_SERVICE_ERROR
        assert output.diagnosis is None
        assert session.get_one(AgentRunRecord, output.run_id).status == "FAILED"
        assert len(session.scalars(select(ToolCallRecord)).all()) == 4
        assert len(session.scalars(select(EvidenceRecord)).all()) == 5
    engine.dispose()


def test_same_case_twice_has_distinct_traces_and_fresh_replay_state(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    client = ScriptedModelClient(
        [*_model_steps(), *_model_steps(first_evidence=6)]
    )
    with Session(engine) as session:
        runtime = _runtime(session, client, SequentialIds())
        first = asyncio.run(runtime.run(REQUEST))
        second = asyncio.run(runtime.run(REQUEST))
        assert first.status is second.status is AgentStatus.COMPLETED
        assert first.task_id != second.task_id
        assert first.run_id != second.run_id
        assert first.trace_id != second.trace_id
        assert client.call_count == 6
        assert len(session.scalars(select(AgentRunRecord)).all()) == 2
        assert len(session.scalars(select(ToolCallRecord)).all()) == 8
        assert len(session.scalars(select(EvidenceRecord)).all()) == 10
    engine.dispose()


def test_live_mode_uses_same_runtime_with_injected_reader(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    live_request = DiagnosisRequest(
        query=REQUEST.query, namespace=REQUEST.namespace, mode="live",
    )
    with Session(engine) as session:
        runtime = _runtime(
            session, ScriptedModelClient(_model_steps()), SequentialIds(),
            registry_factory=lambda request: build_kubernetes_registry(CaseReader()),
        )
        output = asyncio.run(runtime.run(live_request))
        assert output.status is AgentStatus.COMPLETED, output.error
        assert output.diagnosis is not None
        assert len(session.scalars(select(ToolCallRecord)).all()) == 4
    engine.dispose()


def test_router_token_budget_stops_before_planner_model_call(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    client = ScriptedModelClient(_model_steps()[:1])
    with Session(engine) as session:
        output = asyncio.run(_runtime(
            session, client, SequentialIds(),
            budget_limits=BudgetLimits(max_tokens=50),
        ).run(REQUEST))
        assert output.status is AgentStatus.BUDGET_EXCEEDED
        assert output.error is not None and output.error.code is ErrorCode.BUDGET_EXCEEDED
        assert output.state.budget.tokens_used == 50
        assert client.call_count == 1
        assert [item.component for item in session.scalars(select(LLMCallRecord)).all()] == ["router"]
        assert session.scalars(select(ToolCallRecord)).all() == []
    engine.dispose()


def test_runtime_replay_call_id_mismatch_is_policy_failure(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    steps = _model_steps()
    wrong_plan = _plan().model_dump(mode="json")
    wrong_plan["steps"][0]["call_id"] = "call_wrong_status"
    client = ScriptedModelClient([steps[0], ScriptedModelResponse(payload=wrong_plan)])
    with Session(engine) as session:
        output = asyncio.run(_runtime(session, client, SequentialIds()).run(REQUEST))
        assert output.status is AgentStatus.POLICY_REJECTED
        assert output.error is not None and output.error.code is ErrorCode.POLICY_REJECTED
        assert client.call_count == 2
        calls = session.scalars(select(ToolCallRecord)).all()
        assert len(calls) == 1 and calls[0].error_code == ErrorCode.POLICY_REJECTED.value
    engine.dispose()


def test_run_is_persisted_as_executing_before_first_tool_call(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    with Session(engine) as session:
        class ObservedReplayRegistry(ReplayToolRegistry):
            async def invoke(self, invocation: ToolInvocation) -> ToolResponse:
                run = session.get_one(AgentRunRecord, "run_001")
                assert run.status == "EXECUTING"
                return await super().invoke(invocation)

        runtime = _runtime(
            session, ScriptedModelClient(_model_steps()), SequentialIds(),
            registry_factory=lambda request: ObservedReplayRegistry(load_case(CASE_FILE)),
        )
        output = asyncio.run(runtime.run(REQUEST))
        assert output.status is AgentStatus.COMPLETED, output.error
    engine.dispose()
