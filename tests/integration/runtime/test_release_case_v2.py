import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from pydantic import JsonValue
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot.agent.state import AgentStatus
from opspilot.cases import load_case_v2
from opspilot.diagnosis.release_v2 import PostDeploymentVerifierV2
from opspilot.llm.client import ScriptedModelClient
from opspilot.llm.models import ScriptedModelResponse, StructuredModelConfig
from opspilot.llm.prompts import load_prompt
from opspilot.planning.v2_planner import V2Planner
from opspilot.routing.v2 import V2IntentRouter
from opspilot.runtime.replay_v2 import ReplayV2Registry
from opspilot.runtime.v2 import ObservationRuntimeV2, V2DiagnosisRequest
from opspilot.storage import (
    EvidenceRecord, SQLAlchemyExecutionRepository,
    SQLAlchemyModelAuditRepository, SQLAlchemyPlanningRoundRepository,
    SQLAlchemyRuntimeRepository, ToolCallRecord,
)

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "fixtures/cases/release-failure-v2/case.json"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
BASE = "a" * 40
HEAD = "b" * 40


def _response(payload: dict[str, object]) -> ScriptedModelResponse:
    return ScriptedModelResponse(
        payload=cast(dict[str, JsonValue], payload), input_tokens=10, output_tokens=5,
    )


def _decision(
    round_no: int, evidence_ids: list[str], calls: list[dict[str, object]],
    action: str = "continue",
) -> ScriptedModelResponse:
    return _response({
        "schema_version": 2, "round_no": round_no, "action": action,
        "based_on_evidence_ids": evidence_ids, "calls": calls,
    })


def _call(identifier: str, tool: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"call_id": identifier, "tool": tool, "arguments": arguments,
            "reason": "Read bounded release evidence"}


def test_release_case_replay_audits_timeline_without_causal_upgrade(tmp_path: Path) -> None:
    case = load_case_v2(CASE)
    branch_id = "post_release_rise"
    assert case.definition.candidate_families == ("post_deployment_failure",)
    url = f"sqlite+pysqlite:///{(tmp_path / 'release.db').as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    ids: dict[str, int] = {}

    def next_id(kind: str) -> str:
        ids[kind] = ids.get(kind, 0) + 1
        return f"{kind}_{ids[kind]:03d}"

    script = [
        _response({"intent": "diagnose", "domain": "kubernetes",
                   "fault_family": "post_deployment_failure", "target_kind": "deployment",
                   "resource": "api"}),
        _decision(1, [], [
            _call("call_releases", "cicd.get_recent_deployment",
                  {"namespace": "team-a", "deployment_name": "api"}),
            _call("call_error_rate", "prometheus.query_error_rate", {
                "namespace": "team-a", "workload_name": "api",
                "window_seconds": 3600, "step_seconds": 60, "max_samples": 100,
            }),
        ]),
        _decision(2, [f"ev_{number:03d}" for number in range(1, 7)], [
            _call("call_commit", "git.get_recent_commit", {
                "namespace": "team-a", "deployment_name": "api", "commit_sha": HEAD,
            }),
            _call("call_diff", "git.diff", {
                "namespace": "team-a", "deployment_name": "api",
                "base_sha": BASE, "head_sha": HEAD,
            }),
        ]),
        _decision(3, [f"ev_{number:03d}" for number in range(1, 9)], [], "finish"),
    ]
    request = V2DiagnosisRequest(
        query=case.definition.query, namespace="team-a", mode="replay",
        case_id=case.definition.case_id, branch_id=branch_id,
    )
    registry = ReplayV2Registry(case, branch_id)
    with Session(engine) as session:
        storage = SQLAlchemyRuntimeRepository(session)
        audit = SQLAlchemyModelAuditRepository(session)
        tools = SQLAlchemyExecutionRepository(session)
        rounds = SQLAlchemyPlanningRoundRepository(session)
        client = ScriptedModelClient(script)
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
            verifier=PostDeploymentVerifierV2(),
            task_repository=storage, run_repository=storage,
            tool_repository=tools, round_repository=rounds,
            result_repository=storage, registry_factory=lambda _: registry,
            allowed_resources=lambda _: frozenset(case.definition.allowed_resource_names),
            id_factory=next_id, clock=lambda: NOW,
        )
        outcome = asyncio.run(runtime.run(request))
        assert outcome.status is AgentStatus.PARTIAL, outcome.error
        assert outcome.diagnosis is not None
        assert outcome.diagnosis.root_cause == case.branches[0].definition.expected_diagnosis.root_cause
        assert outcome.diagnosis.verification_payload["supported"] is False
        missing = outcome.diagnosis.verification_payload["missing_evidence"]
        assert isinstance(missing, list)
        assert {item["requirement"] for item in cast(list[dict[str, str]], missing)} == {
            "deployment_snapshot", "workload_revision_binding", "direct_causality",
        }
        registry.assert_complete()
        client.assert_exhausted()
        assert len(rounds.rounds_for_run(outcome.run_id)) == 3
        assert len(session.scalars(select(ToolCallRecord).where(
            ToolCallRecord.run_id == outcome.run_id
        )).all()) == 4
        assert len(session.scalars(select(EvidenceRecord).where(
            EvidenceRecord.run_id == outcome.run_id
        )).all()) == 8
    engine.dispose()
