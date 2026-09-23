import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot.agent import BudgetState, IntentOutput, Target
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.cases import load_case
from opspilot.diagnosis import DiagnosisDraftV1, V1DiagnosisAssembler
from opspilot.execution import BoundedExecutor
from opspilot.llm.client import ScriptedModelClient
from opspilot.llm.models import ScriptedModelResponse, StructuredModelConfig
from opspilot.llm.prompts import load_prompt
from opspilot.planning import PlanValidator, ValidatedPlanV1
from opspilot.storage import (
    AgentRunRecord, Base, DiagnosisTaskRecord, EvidenceRecord, LLMCallRecord,
    SQLAlchemyExecutionRepository, SQLAlchemyModelAuditRepository,
    SQLAlchemyRuntimeRepository,
)
from opspilot.tools.kubernetes import build_kubernetes_registry
from tests.integration.execution.test_case_execution import _plan
from tests.integration.tools.test_kubernetes_registry import CaseReader

ROOT = Path(__file__).resolve().parents[3]
CASE_FILE = ROOT / "fixtures/cases/crashloop-liveness-v1/case.json"


def test_v0_case_replay_persists_verified_v1_result_by_trace(tmp_path: Path) -> None:
    case = load_case(CASE_FILE)
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'diagnosis.db').as_posix()}")
    Base.metadata.create_all(engine)
    at = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([
            DiagnosisTaskRecord(id="task_001", user_query="Why is slow-start-api restarting?", namespace=case.definition.target.namespace, status="PLANNING"),
            AgentRunRecord(id="run_001", task_id="task_001", trace_id=case.definition.trace_id, attempt_no=1, status="PLANNING", state_payload={"status": "PLANNING"}, runtime_version="v1"),
        ])
        session.commit()
        registry = build_kubernetes_registry(CaseReader())
        intent = IntentOutput(
            intent="diagnose", domain="kubernetes", problem_type="pod_restart",
            target=Target(namespace=case.definition.target.namespace, resource=case.definition.target.resource),
        )
        validation = PlanValidator(registry).validate(_plan(), intent=intent, budget=BudgetState())
        assert isinstance(validation, ValidatedPlanV1)
        state = AgentState(
            task_id="task_001", trace_id=case.definition.trace_id,
            user_query="Why is slow-start-api restarting?", intent=intent,
            execution_plan_v1=validation.plan, status=AgentStatus.PLANNING,
            created_at=at, updated_at=at,
        )
        tool_ids = iter(f"tool_{index:03d}" for index in range(1, 10))
        evidence_ids = iter(f"ev_{index:03d}" for index in range(1, 20))
        execution_repository = SQLAlchemyExecutionRepository(session)
        executor = BoundedExecutor(
            registry=registry, repository=execution_repository, clock=lambda: at,
            tool_attempt_id_factory=lambda: next(tool_ids),
            evidence_id_factory=lambda: next(evidence_ids),
        )
        summary = asyncio.run(executor.execute(run_id="run_001", state=state, validated_plan=validation))
        assert summary.error is None
        evidence = execution_repository.evidence_for_trace(case.definition.trace_id)
        original_rows = session.scalars(select(EvidenceRecord).order_by(EvidenceRecord.id)).all()
        original_contents = {item.id: item.content for item in original_rows}
        claim = case.definition.expected_diagnosis.claim.model_copy(
            update={"evidence_ids": tuple(item.evidence_id for item in evidence)}
        )
        draft = DiagnosisDraftV1(
            schema_version=1,
            root_cause=case.definition.expected_diagnosis.root_cause,
            recommendation=case.definition.expected_diagnosis.recommendation,
            claims=(claim,),
        )
        client = ScriptedModelClient([ScriptedModelResponse(payload=draft.model_dump(mode="json"))])
        runtime_repository = SQLAlchemyRuntimeRepository(session)
        runtime_repository.save_state("run_001", summary.state)
        assembler = V1DiagnosisAssembler(
            client=client, audit_repository=SQLAlchemyModelAuditRepository(session),
            evidence_repository=execution_repository,
            run_repository=runtime_repository,
            result_repository=runtime_repository,
            prompt=load_prompt(ROOT / "prompts/diagnosis/v1.md", component="diagnosis", version="v1"),
            model_config=StructuredModelConfig(provider="test", model="scripted"),
        )
        outcome = asyncio.run(assembler.diagnose(run_id="run_001", result_id="result_001", execution=summary))
        saved = runtime_repository.get_result_for_trace(case.definition.trace_id)
        assert saved is not None
        assert saved.status == case.definition.expected_diagnosis.status
        assert saved.root_cause == case.definition.expected_diagnosis.root_cause
        assert saved.recommendation == case.definition.expected_diagnosis.recommendation
        assert saved.verification_payload["supported"] is True
        assert saved.claims_payload[0]["evidence_ids"] == [item.evidence_id for item in evidence]
        assert outcome.assessment.verification.checked_evidence_ids
        assert {item.id: item.content for item in session.scalars(select(EvidenceRecord)).all()} == original_contents
        calls = session.scalars(select(LLMCallRecord).where(LLMCallRecord.run_id == "run_001")).all()
        assert len(calls) == 1 and calls[0].component == "diagnosis" and calls[0].success
    engine.dispose()
