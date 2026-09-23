import asyncio
from collections.abc import Iterator
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot.agent import BudgetState, IntentOutput, Target
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.integrations.kubernetes import KubernetesReader
from opspilot.llm import (
    ModelAuditError,
    PromptTemplate,
    ScriptedModelClient,
    ScriptedModelResponse,
    StructuredModelConfig,
    load_prompt,
)
from opspilot.routing import IntentRouter
from opspilot.planning import PlanValidator, V1Planner
from opspilot.tools.kubernetes import build_kubernetes_registry
from opspilot.storage import (
    AgentRunRecord,
    Base,
    DiagnosisTaskRecord,
    LLMCallRecord,
    PromptVersionRecord,
    SQLAlchemyModelAuditRepository,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        task = DiagnosisTaskRecord(
            id="task_001",
            user_query="Why does slow-start-api keep restarting?",
            namespace="opspilot-fixtures",
            status="ROUTING",
        )
        run = AgentRunRecord(
            id="run_001",
            task_id=task.id,
            trace_id="trace_001",
            attempt_no=1,
            status="ROUTING",
            state_payload={"status": "ROUTING"},
            runtime_version="v1",
        )
        database_session.add_all([task, run])
        database_session.commit()
        yield database_session
    engine.dispose()


def test_router_persists_prompt_and_every_model_attempt(
    session: Session,
) -> None:
    repository = SQLAlchemyModelAuditRepository(session)
    prompt = load_prompt(
        ROOT / "prompts" / "router" / "v1.md",
        component="router",
        version="v1",
    )
    client = ScriptedModelClient(
        [
            ScriptedModelResponse(
                payload={"invalid": True},
                input_tokens=10,
                output_tokens=3,
                cost_usd=Decimal("0.000100"),
                latency_ms=4,
            ),
            ScriptedModelResponse(
                payload={
                    "intent": "diagnose",
                    "domain": "kubernetes",
                    "problem_type": "pod_restart",
                    "resource": "slow-start-api",
                },
                input_tokens=12,
                output_tokens=5,
                cost_usd=Decimal("0.000200"),
                latency_ms=6,
            ),
        ]
    )
    router = IntentRouter(
        client=client,
        audit_repository=repository,
        prompt=prompt,
        model_config=StructuredModelConfig(
            provider="scripted",
            model="router-test-model",
        ),
    )

    outcome = asyncio.run(
        router.route(
            run_id="run_001",
            query="Why does slow-start-api keep restarting?",
            namespace="opspilot-fixtures",
            budget=BudgetState(),
        )
    )

    prompt_rows = session.scalars(select(PromptVersionRecord)).all()
    calls = session.scalars(
        select(LLMCallRecord).order_by(LLMCallRecord.sequence_no)
    ).all()

    assert len(prompt_rows) == 1
    assert prompt_rows[0].content_hash == prompt.content_hash
    assert prompt_rows[0].template_text == prompt.content
    assert [call.sequence_no for call in calls] == [1, 2]
    assert [call.success for call in calls] == [False, True]
    assert calls[0].error_code == "SCHEMA_VALIDATION"
    assert calls[0].input_tokens == 10
    assert calls[0].output_tokens == 3
    assert calls[0].response_payload is None
    assert calls[1].response_payload is not None
    assert calls[1].cost_usd == Decimal("0.000200")
    assert "Why does" not in str(calls[0].request_payload)
    assert outcome.llm_call_ids == (calls[0].id, calls[1].id)
    assert outcome.budget.tokens_used == 30
    assert outcome.budget.cost_usd == Decimal("0.000300")


def test_prompt_version_cannot_change_content_in_place(
    session: Session,
) -> None:
    repository = SQLAlchemyModelAuditRepository(session)
    first = PromptTemplate(
        component="router",
        version="v1",
        content="first",
        content_hash=sha256(b"first").hexdigest(),
    )
    changed = PromptTemplate(
        component="router",
        version="v1",
        content="changed",
        content_hash=sha256(b"changed").hexdigest(),
    )
    repository.register_prompt(first)

    with pytest.raises(ModelAuditError, match="different content"):
        repository.register_prompt(changed)


def test_planner_persists_rejected_and_accepted_attempts_for_same_run(session: Session) -> None:
    pod: dict[str, JsonValue] = {"namespace": "opspilot-fixtures", "workload_name": "slow-start-api"}
    steps: list[dict[str, JsonValue]] = [
        {"step_id": 1, "call_id": "call_status", "tool": "k8s.get_pod_status", "arguments": pod, "reason": "Read status"},
        {"step_id": 2, "call_id": "call_events", "tool": "k8s.get_pod_events", "arguments": pod, "reason": "Read events"},
        {"step_id": 3, "call_id": "call_previous", "tool": "k8s.get_previous_logs", "arguments": pod, "reason": "Read logs"},
        {"step_id": 4, "call_id": "call_deployment", "tool": "k8s.get_deployment", "arguments": {"namespace": "opspilot-fixtures", "deployment_name": "slow-start-api"}, "reason": "Read probes"},
    ]
    invalid = [{**steps[0], "tool": "k8s.delete_pod"}, *steps[1:]]
    client = ScriptedModelClient(
        [
            ScriptedModelResponse(payload=cast(dict[str, JsonValue], {"schema_version": 1, "steps": invalid})),
            ScriptedModelResponse(payload=cast(dict[str, JsonValue], {"schema_version": 1, "steps": steps})),
        ]
    )
    planner = V1Planner(
        client=client,
        audit_repository=SQLAlchemyModelAuditRepository(session),
        prompt=load_prompt(ROOT / "prompts" / "planner" / "v1.md", component="planner", version="v1"),
        model_config=StructuredModelConfig(provider="scripted", model="planner-test-model"),
    )
    state = AgentState(
        task_id="task_001",
        trace_id="trace_001",
        user_query="Why does slow-start-api keep restarting?",
        intent=IntentOutput(
            intent="diagnose",
            domain="kubernetes",
            problem_type="pod_restart",
            target=Target(namespace="opspilot-fixtures", resource="slow-start-api"),
        ),
        status=AgentStatus.PLANNING,
    )
    outcome = asyncio.run(
        planner.plan(
            run_id="run_001",
            state=state,
            validator=PlanValidator(build_kubernetes_registry(cast(KubernetesReader, object()))),
        )
    )
    calls = session.scalars(select(LLMCallRecord).order_by(LLMCallRecord.sequence_no)).all()
    assert [call.sequence_no for call in calls] == [1, 2]
    assert [call.success for call in calls] == [False, True]
    assert calls[0].error_code == "TOOL_NOT_FOUND"
    assert calls[0].response_payload is not None
    assert calls[0].response_payload["security_event"] is True
    assert calls[1].prompt_version_id == calls[0].prompt_version_id
    assert calls[1].response_payload is not None
    assert calls[1].response_payload["step_count"] == 4
    assert outcome.llm_call_ids == (calls[0].id, calls[1].id)
    assert outcome.state.execution_plan_v1 is not None
