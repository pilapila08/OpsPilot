import asyncio
from datetime import UTC, datetime
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
from opspilot.diagnosis.service_503_v2 import Service503VerifierV2
from opspilot.llm.client import ScriptedModelClient
from opspilot.llm.models import ScriptedModelResponse, StructuredModelConfig
from opspilot.llm.prompts import load_prompt
from opspilot.planning.v2_planner import V2Planner
from opspilot.routing.v2 import V2IntentRouter
from opspilot.runtime.replay_v2 import ReplayV2Registry
from opspilot.runtime.v2 import ObservationRuntimeV2, V2DiagnosisRequest
from opspilot.storage import (
    EvidenceRecord, PlanningRoundRecord, SQLAlchemyExecutionRepository,
    SQLAlchemyModelAuditRepository, SQLAlchemyPlanningRoundRepository,
    SQLAlchemyRuntimeRepository, ToolCallRecord,
)

ROOT = Path(__file__).resolve().parents[3]
CASE_FILE = ROOT / "fixtures/cases/service-503-v2/case.json"
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


class Ids:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def new(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"{kind}_{self.counts[kind]:03d}"


def _response(payload: dict[str, object]) -> ScriptedModelResponse:
    return ScriptedModelResponse(
        payload=cast(dict[str, JsonValue], payload), input_tokens=10, output_tokens=5,
    )


def _call(call_id: str, tool: str, target_key: str, target: str) -> dict[str, object]:
    return {
        "call_id": call_id, "tool": tool,
        "arguments": {"namespace": "team-a", target_key: target},
        "reason": "Read a scoped 503 signal",
    }


@pytest.mark.parametrize(
    ("branch_id", "expected_status"),
    [("no_ready", AgentStatus.COMPLETED), ("healthy_backend", AgentStatus.PARTIAL),
     ("port_anomaly", AgentStatus.PARTIAL),
     ("selector_mismatch", AgentStatus.COMPLETED)],
)
def test_service_503_replay_is_audited_and_evidence_grounded(
    tmp_path: Path, branch_id: str, expected_status: AgentStatus,
) -> None:
    case = load_case_v2(CASE_FILE)
    url = f"sqlite+pysqlite:///{(tmp_path / 'service503.db').as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    calls = (
        _call("call_ingress", "k8s.get_ingress", "ingress_name", "edge"),
        _call("call_metric", "prometheus.query_http_503_rate", "service_name", "api"),
        _call("call_service", "k8s.get_service", "service_name", "api"),
        _call("call_endpoints", "k8s.get_endpoints", "service_name", "api"),
    )
    script = [
        _response({"intent": "diagnose", "domain": "microservice",
                   "fault_family": "service_503", "target_kind": "service", "resource": "api"}),
        _response({"schema_version": 2, "round_no": 1, "action": "continue",
                   "based_on_evidence_ids": [], "calls": list(calls[:2])}),
        _response({"schema_version": 2, "round_no": 2, "action": "continue",
                   "based_on_evidence_ids": ["ev_001", "ev_002"], "calls": list(calls[2:])}),
    ]
    expected_calls = list(calls)
    final_round = 3
    final_ids = ["ev_001", "ev_002", "ev_003", "ev_004"]
    if branch_id in {"port_anomaly", "selector_mismatch"}:
        membership = _call("call_membership", "k8s.get_service_membership", "service_name", "api")
        membership["arguments"] = {"namespace": "team-a", "service_name": "api", "deployment_name": "api"}
        expected_calls.append(membership)
        script.append(_response({"schema_version": 2, "round_no": 3, "action": "continue",
                                 "based_on_evidence_ids": final_ids, "calls": [membership]}))
        final_round = 4
        final_ids = [*final_ids, "ev_005"]
    script.append(_response({"schema_version": 2, "round_no": final_round, "action": "finish",
                             "based_on_evidence_ids": final_ids, "calls": []}))
    client = ScriptedModelClient(script)
    model = StructuredModelConfig(provider="test", model="scripted")
    request = V2DiagnosisRequest(
        query=case.definition.query, namespace="team-a", mode="replay",
        case_id=case.definition.case_id, branch_id=branch_id,
    )
    with Session(engine) as session:
        storage = SQLAlchemyRuntimeRepository(session)
        audit = SQLAlchemyModelAuditRepository(session)
        tools = SQLAlchemyExecutionRepository(session)
        rounds = SQLAlchemyPlanningRoundRepository(session)
        registry = ReplayV2Registry(case, branch_id)
        runtime = ObservationRuntimeV2(
            router=V2IntentRouter(
                client=client, audit_repository=audit,
                prompt=load_prompt(ROOT / "prompts/router/v2.md", component="router", version="v2"),
                model_config=model,
            ),
            planner=V2Planner(
                client=client, audit_repository=audit,
                prompt=load_prompt(ROOT / "prompts/planner/v2.md", component="planner", version="v2"),
                model_config=model,
            ),
            verifier=Service503VerifierV2(), task_repository=storage,
            run_repository=storage, tool_repository=tools,
            round_repository=rounds, result_repository=storage,
            registry_factory=lambda _: registry,
            allowed_resources=lambda _: frozenset(case.definition.allowed_resource_names),
            id_factory=Ids().new, clock=lambda: NOW,
        )
        result = asyncio.run(runtime.run(request))
        assert result.status is expected_status, result.error
        assert result.diagnosis is not None
        expected = next(item.definition.expected_diagnosis for item in case.branches
                        if item.definition.branch_id == branch_id)
        assert result.diagnosis.root_cause == expected.root_cause
        assert result.diagnosis.verification_payload["supported"] is (expected_status is AgentStatus.COMPLETED)
        assert result.diagnosis.claims_payload[0]["claim_id"] == expected.claim.claim_id
        if branch_id == "port_anomaly":
            missing = cast(list[dict[str, JsonValue]],
                           result.diagnosis.verification_payload["missing_evidence"])
            assert missing[0]["requirement"] == "port_causality"
        assert [item.tool_name for item in session.scalars(
            select(ToolCallRecord).where(ToolCallRecord.run_id == result.run_id)
            .order_by(ToolCallRecord.sequence_no)
        )] == [item["tool"] for item in expected_calls]
        assert len(session.scalars(select(EvidenceRecord).where(
            EvidenceRecord.run_id == result.run_id,
        )).all()) == len(final_ids)
        assert len(session.scalars(select(PlanningRoundRecord).where(
            PlanningRoundRecord.run_id == result.run_id,
        )).all()) == final_round
        registry.assert_complete()
        client.assert_exhausted()
    engine.dispose()
