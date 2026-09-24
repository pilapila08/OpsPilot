import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from pydantic import JsonValue
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot.agent.state import AgentStatus
from opspilot.cases import load_case_v2
from opspilot.diagnosis.v2 import V2Assessment
from opspilot.diagnosis.oom_v2 import OomKilledVerifierV2
from opspilot.evidence.models import Claim, Evidence, MissingEvidence, Verification
from opspilot.llm.client import ScriptedModelClient
from opspilot.llm.models import ScriptedModelResponse, StructuredModelConfig
from opspilot.llm.prompts import load_prompt
from opspilot.planning.v2_planner import V2Planner
from opspilot.routing.v2 import IntentV2, V2IntentRouter
from opspilot.runtime.replay_v2 import ReplayV2Registry
from opspilot.runtime.v2 import ObservationRuntimeV2, V2DiagnosisRequest
from opspilot.storage import (
    AgentRunRecord, EvidenceRecord, LLMCallRecord,
    PlanningRoundRecord, SQLAlchemyExecutionRepository,
    SQLAlchemyModelAuditRepository, SQLAlchemyPlanningRoundRepository,
    SQLAlchemyRuntimeRepository, ToolCallRecord,
)

ROOT = Path(__file__).resolve().parents[3]
CASE_FILE = ROOT / "fixtures/cases/restart-branches-v2/case.json"


class SequentialIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def new(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"{kind}_{self.counts[kind]:03d}"


class PartialVerifier:
    def verify(
        self, *, intent: IntentV2, evidence: tuple[Evidence, ...],
        force_partial: bool,
    ) -> V2Assessment:
        del intent, force_partial
        first = evidence[0].evidence_id
        return V2Assessment(
            status="PARTIAL", root_cause="A restart is observed; the specific cause remains unverified.",
            recommendation="Inspect the missing corroborating signal before changing configuration.",
            confidence=Decimal("0.4"),
            claim=Claim(
                claim_id="claim_restart_observed", text="The target restarted.",
                evidence_ids=(first,), inference_confidence=0.5,
            ),
            verification=Verification(
                claim_id="claim_restart_observed", supported=False,
                verification_confidence=0.4,
                checked_evidence_ids=tuple(item.evidence_id for item in evidence),
                missing_evidence=(MissingEvidence(
                    requirement="fault-specific corroboration",
                    reason="This fake rule does not assert a specific root cause.",
                ),),
                rationale="Only a bounded observation path is under test.",
            ),
        )


def _response(payload: dict[str, object]) -> ScriptedModelResponse:
    return ScriptedModelResponse(
        payload=cast(dict[str, JsonValue], payload),
        input_tokens=10, output_tokens=5,
    )


def _decision(
    round_no: int, evidence_ids: list[str], *,
    call_id: str | None = None, tool: str | None = None,
    arguments: dict[str, object] | None = None,
    action: str = "continue",
) -> dict[str, object]:
    calls: list[dict[str, object]] = []
    if call_id is not None and tool is not None and arguments is not None:
        calls.append({
            "call_id": call_id, "tool": tool, "arguments": arguments,
            "reason": "Observe next bounded signal",
        })
    return {
        "schema_version": 2, "round_no": round_no, "action": action,
        "based_on_evidence_ids": evidence_ids, "calls": calls,
    }


def _script(branch_id: str) -> list[ScriptedModelResponse]:
    second_tool = "k8s.get_deployment" if branch_id == "oom_branch" else "k8s.get_pod_events"
    second_call = "call_oom_deployment" if branch_id == "oom_branch" else "call_probe_events"
    second_args: dict[str, object] = {
        "namespace": "opspilot-fixtures",
        ("deployment_name" if branch_id == "oom_branch" else "workload_name"): "api",
    }
    steps = [
        _response({
            "intent": "diagnose", "domain": "kubernetes", "fault_family": "oom_killed",
            "target_kind": "deployment", "resource": "api",
        }),
        _response(_decision(1, [], call_id="call_status", tool="k8s.get_pod_status", arguments={
            "namespace": "opspilot-fixtures", "workload_name": "api",
        })),
        _response(_decision(2, ["ev_001"], call_id=second_call, tool=second_tool, arguments=second_args)),
    ]
    if branch_id == "oom_branch":
        steps.append(_response(_decision(3, ["ev_001", "ev_002", "ev_003"], action="partial")))
    else:
        steps.append(_response(_decision(3, ["ev_001", "ev_002"], action="partial")))
    return steps


@pytest.mark.parametrize(
    ("branch_id", "expected_tools", "expected_rounds"),
    [
        ("oom_branch", ["k8s.get_pod_status", "k8s.get_deployment"], 3),
        ("probe_branch", ["k8s.get_pod_status", "k8s.get_pod_events"], 3),
    ],
)
def test_v2_observation_changes_next_tool_and_reconstructs_trace(
    tmp_path: Path, branch_id: str, expected_tools: list[str], expected_rounds: int,
) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'v2.db').as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    case = load_case_v2(CASE_FILE)
    request = V2DiagnosisRequest(
        query=case.definition.query, namespace=case.definition.target.namespace,
        mode="replay", case_id=case.definition.case_id, branch_id=branch_id,
    )
    registry = ReplayV2Registry(case, branch_id)
    with Session(engine) as session:
        storage = SQLAlchemyRuntimeRepository(session)
        audit = SQLAlchemyModelAuditRepository(session)
        tools = SQLAlchemyExecutionRepository(session)
        rounds = SQLAlchemyPlanningRoundRepository(session)
        client = ScriptedModelClient(_script(branch_id))
        model_config = StructuredModelConfig(provider="test", model="scripted")
        runtime = ObservationRuntimeV2(
            router=V2IntentRouter(
                client=client, audit_repository=audit,
                prompt=load_prompt(ROOT / "prompts/router/v2.md", component="router", version="v2"),
                model_config=model_config,
            ),
            planner=V2Planner(
                client=client, audit_repository=audit,
                prompt=load_prompt(ROOT / "prompts/planner/v2.md", component="planner", version="v2"),
                model_config=model_config,
            ),
            verifier=PartialVerifier(),
            task_repository=storage, run_repository=storage,
            tool_repository=tools, round_repository=rounds,
            result_repository=storage,
            registry_factory=lambda _: registry,
            allowed_resources=lambda _: frozenset(case.definition.allowed_resource_names),
            id_factory=SequentialIds().new,
        )
        output = asyncio.run(runtime.run(request))
        assert output.status is AgentStatus.PARTIAL, output.error
        assert output.diagnosis is not None
        assert output.diagnosis.schema_version == 2
        assert output.diagnosis.verification_payload["supported"] is False
        registry.assert_complete()
        client.assert_exhausted()
        second_round_input = client.requests[2].messages[1].content
        assert ("OOMKilled" if branch_id == "oom_branch" else '"Error"') in second_round_input
        assert "raw_result_ref" not in second_round_input
        run = session.get_one(AgentRunRecord, output.run_id)
        assert run.runtime_version == "v2"
        assert [item.tool_name for item in session.scalars(
            select(ToolCallRecord).where(ToolCallRecord.run_id == output.run_id)
            .order_by(ToolCallRecord.sequence_no)
        )] == expected_tools
        persisted_rounds = rounds.rounds_for_run(output.run_id)
        assert len(persisted_rounds) == expected_rounds
        assert persisted_rounds[0].evidence_ids == ()
        assert persisted_rounds[1].evidence_ids == (
            ("ev_001", "ev_002") if branch_id == "oom_branch" else ("ev_001",)
        )
        assert all(item.validation_status == "ADMITTED" for item in persisted_rounds)
        assert len(session.scalars(select(LLMCallRecord).where(LLMCallRecord.run_id == output.run_id)).all()) == expected_rounds + 1
        assert len(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == output.run_id)).all()) >= 1
        assert len(session.scalars(select(PlanningRoundRecord).where(PlanningRoundRecord.run_id == output.run_id)).all()) == expected_rounds
        assert storage.get_run(output.run_id) is not None
    engine.dispose()


@pytest.mark.parametrize(
    ("branch_id", "expected_status", "metric_ids"),
    [
        ("peak_present", AgentStatus.COMPLETED, ["ev_004", "ev_005"]),
        ("peak_missing", AgentStatus.PARTIAL, ["ev_004"]),
    ],
)
def test_oom_case_replay_is_evidence_grounded_and_audited(
    tmp_path: Path, branch_id: str, expected_status: AgentStatus,
    metric_ids: list[str],
) -> None:
    case = load_case_v2(CASE_FILE.with_name("oom-limit-v2.json"))
    url = f"sqlite+pysqlite:///{(tmp_path / 'oom.db').as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    model_config = StructuredModelConfig(provider="test", model="scripted")
    request = V2DiagnosisRequest(
        query=case.definition.query, namespace=case.definition.target.namespace,
        mode="replay", case_id=case.definition.case_id, branch_id=branch_id,
    )
    calls: list[tuple[int, list[str], str, str, dict[str, object]]] = [
        (1, [], "call_status", "k8s.get_pod_status", {
            "namespace": "opspilot-fixtures", "workload_name": "api",
        }),
        (2, ["ev_001", "ev_002"], "call_oom_deployment", "k8s.get_deployment", {
            "namespace": "opspilot-fixtures", "deployment_name": "api",
        }),
        (3, ["ev_001", "ev_002", "ev_003"], "call_oom_memory", "prometheus.query_memory", {
            "namespace": "opspilot-fixtures", "workload_name": "api",
            "pod_name": "api-001", "container_name": "api",
        }),
    ]
    script = [_response({
        "intent": "diagnose", "domain": "kubernetes", "fault_family": "oom_killed",
        "target_kind": "deployment", "resource": "api",
    })]
    script.extend(_response(_decision(round_no, evidence_ids, call_id=call_id, tool=tool, arguments=args))
                  for round_no, evidence_ids, call_id, tool, args in calls)
    script.append(_response(_decision(4, ["ev_001", "ev_002", "ev_003", *metric_ids], action="finish")))
    with Session(engine) as session:
        storage = SQLAlchemyRuntimeRepository(session)
        audit = SQLAlchemyModelAuditRepository(session)
        tools = SQLAlchemyExecutionRepository(session)
        rounds = SQLAlchemyPlanningRoundRepository(session)
        client = ScriptedModelClient(script)
        registry = ReplayV2Registry(case, branch_id)
        runtime = ObservationRuntimeV2(
            router=V2IntentRouter(
                client=client, audit_repository=audit,
                prompt=load_prompt(ROOT / "prompts/router/v2.md", component="router", version="v2"),
                model_config=model_config,
            ),
            planner=V2Planner(
                client=client, audit_repository=audit,
                prompt=load_prompt(ROOT / "prompts/planner/v2.md", component="planner", version="v2"),
                model_config=model_config,
            ),
            verifier=OomKilledVerifierV2(), task_repository=storage,
            run_repository=storage, tool_repository=tools,
            round_repository=rounds, result_repository=storage,
            registry_factory=lambda _: registry,
            allowed_resources=lambda _: frozenset(case.definition.allowed_resource_names),
            id_factory=SequentialIds().new,
            clock=lambda: datetime(2026, 9, 23, 10, 0, 30, tzinfo=UTC),
        )
        output = asyncio.run(runtime.run(request))
        assert output.status is expected_status, output.error
        assert output.diagnosis is not None
        branch = next(item.definition for item in case.branches if item.definition.branch_id == branch_id)
        assert output.diagnosis.root_cause == branch.expected_diagnosis.root_cause
        assert output.diagnosis.verification_payload["supported"] is (expected_status is AgentStatus.COMPLETED)
        assert len(rounds.rounds_for_run(output.run_id)) == 4
        assert [item.tool_name for item in session.scalars(
            select(ToolCallRecord).where(ToolCallRecord.run_id == output.run_id)
            .order_by(ToolCallRecord.sequence_no)
        )] == ["k8s.get_pod_status", "k8s.get_deployment", "prometheus.query_memory"]
        assert len(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == output.run_id)).all()) == 3 + len(metric_ids)
        assert len(session.scalars(select(LLMCallRecord).where(LLMCallRecord.run_id == output.run_id)).all()) == 5
        registry.assert_complete()
        client.assert_exhausted()
    engine.dispose()
