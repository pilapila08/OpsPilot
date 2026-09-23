import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from opspilot.agent import BudgetLimits, BudgetState, IntentOutput, Target
from opspilot.agent.state import AgentState, AgentStatus
from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes import KubernetesReader
from opspilot.llm import (
    InMemoryModelAuditRepository,
    ModelBudgetError,
    ModelSchemaError,
    ModelTimeoutError,
    ScriptedModelClient,
    ScriptedModelResponse,
    StructuredModelConfig,
    load_prompt,
)
from opspilot.planning import PlanRejectedError, PlanValidator, V1Planner
from opspilot.tools.kubernetes import build_kubernetes_registry

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "prompts" / "planner" / "v1.md"
PROMPT_HASH = "dc0753ca9bc4891a96241e9e89b435d44ce3949b14c4849e81d3a01bb8318d54"


def _state(*, budget: BudgetState | None = None) -> AgentState:
    intent = IntentOutput(
        intent="diagnose",
        domain="kubernetes",
        problem_type="pod_restart",
        target=Target(namespace="opspilot-fixtures", resource="slow-start-api"),
    )
    return AgentState(
        task_id="task_001",
        trace_id="trace_001",
        user_query="Why is slow-start-api restarting?",
        intent=intent,
        budget=budget or BudgetState(),
        status=AgentStatus.PLANNING,
    )


def _plan_payload() -> dict[str, object]:
    pod = {"namespace": "opspilot-fixtures", "workload_name": "slow-start-api"}
    return {
        "schema_version": 1,
        "steps": [
            {"step_id": 1, "call_id": "call_status", "tool": "k8s.get_pod_status", "arguments": pod, "reason": "Read container state"},
            {"step_id": 2, "call_id": "call_events", "tool": "k8s.get_pod_events", "arguments": pod, "reason": "Read probe events"},
            {"step_id": 3, "call_id": "call_previous", "tool": "k8s.get_previous_logs", "arguments": pod, "reason": "Read terminated process logs"},
            {"step_id": 4, "call_id": "call_deployment", "tool": "k8s.get_deployment", "arguments": {"namespace": "opspilot-fixtures", "deployment_name": "slow-start-api"}, "reason": "Read liveness probe configuration"},
        ],
    }


def _response(payload: dict[str, object]) -> ScriptedModelResponse:
    return ScriptedModelResponse(
        payload=cast(dict[str, JsonValue], payload),
        input_tokens=12,
        output_tokens=8,
        cost_usd=Decimal("0.001000"),
        latency_ms=25,
    )


def _planner(client: ScriptedModelClient, audit: InMemoryModelAuditRepository) -> V1Planner:
    return V1Planner(
        client=client,
        audit_repository=audit,
        prompt=load_prompt(PROMPT_PATH, component="planner", version="v1"),
        model_config=StructuredModelConfig(provider="scripted", model="planner-test-model"),
    )


def _validator() -> PlanValidator:
    return PlanValidator(build_kubernetes_registry(cast(KubernetesReader, object())))


def test_planner_prompt_snapshot_and_untrusted_data_instruction() -> None:
    prompt = load_prompt(PROMPT_PATH, component="planner", version="v1")
    assert prompt.content_hash == PROMPT_HASH
    assert "untrusted data" in prompt.content
    assert "Never obey instructions" in prompt.content


def test_planner_accepts_case_plan_records_attempt_and_updates_state() -> None:
    client = ScriptedModelClient([_response(_plan_payload())])
    audit = InMemoryModelAuditRepository()
    original = _state()
    outcome = asyncio.run(
        _planner(client, audit).plan(run_id="run_001", state=original, validator=_validator())
    )

    assert original.execution_plan_v1 is None
    assert outcome.state.execution_plan_v1 == outcome.validated_plan.plan
    assert outcome.state.plan is None
    assert outcome.state.budget.tokens_used == 20
    assert outcome.attempts == 1
    assert outcome.llm_call_ids == ("llm_run_001_1",)
    assert client.output_models == (type(outcome.validated_plan.plan),)
    assert client.requests[0].prompt.content_hash == PROMPT_HASH
    data = json.loads(client.requests[0].messages[1].content)
    assert len(data["tool_descriptors"]) == 5
    assert "handler" not in client.requests[0].messages[1].content
    assert "kubeconfig" not in client.requests[0].messages[1].content
    assert audit.attempts[0].success is True
    assert audit.attempts[0].run_id == "run_001"
    assert audit.attempts[0].response_payload is not None
    assert audit.attempts[0].response_payload["step_count"] == 4
    assert "Read container state" not in audit.attempts[0].model_dump_json()
    assert outcome.state == AgentState.model_validate_json(outcome.state.model_dump_json())


def test_schema_failure_regenerates_and_consumes_retry_budget() -> None:
    client = ScriptedModelClient([_response({"schema_version": 1}), _response(_plan_payload())])
    audit = InMemoryModelAuditRepository()
    outcome = asyncio.run(
        _planner(client, audit).plan(run_id="run_001", state=_state(), validator=_validator())
    )
    assert outcome.attempts == 2
    assert outcome.state.budget.retries_used == 1
    assert outcome.state.budget.tokens_used == 40
    assert [item.error_code for item in audit.attempts] == [ErrorCode.SCHEMA_VALIDATION, None]
    assert "schema" in client.requests[1].messages[-1].content


def test_semantic_failure_regenerates_once_and_records_security_event() -> None:
    invalid = _plan_payload()
    steps = cast(list[dict[str, object]], invalid["steps"])
    steps[0]["tool"] = "k8s.delete_pod"
    client = ScriptedModelClient([_response(invalid), _response(_plan_payload())])
    audit = InMemoryModelAuditRepository()
    outcome = asyncio.run(
        _planner(client, audit).plan(run_id="run_001", state=_state(), validator=_validator())
    )
    assert outcome.attempts == 2
    assert outcome.state.budget.retries_used == 1
    assert audit.attempts[0].error_code is ErrorCode.TOOL_NOT_FOUND
    assert audit.attempts[0].response_payload is not None
    assert audit.attempts[0].response_payload["security_event"] is True
    assert "k8s.delete_pod" not in client.requests[1].messages[-1].content


def test_repeated_invalid_semantic_plan_stops_after_one_regeneration() -> None:
    invalid = _plan_payload()
    steps = cast(list[dict[str, object]], invalid["steps"])
    steps[0]["arguments"] = {"namespace": "prod", "workload_name": "slow-start-api"}
    client = ScriptedModelClient([_response(invalid), _response(invalid)])
    audit = InMemoryModelAuditRepository()
    with pytest.raises(PlanRejectedError) as exc_info:
        asyncio.run(_planner(client, audit).plan(run_id="run_001", state=_state(), validator=_validator()))
    assert exc_info.value.error.code is ErrorCode.POLICY_REJECTED
    assert client.call_count == 2
    assert [item.success for item in audit.attempts] == [False, False]


def test_schema_failure_stops_after_two_regenerations() -> None:
    client = ScriptedModelClient([_response({"invalid": 1}) for _ in range(3)])
    audit = InMemoryModelAuditRepository()
    with pytest.raises(ModelSchemaError):
        asyncio.run(_planner(client, audit).plan(run_id="run_001", state=_state(), validator=_validator()))
    assert client.call_count == 3
    assert len(audit.attempts) == 3


def test_budget_exceeded_plan_does_not_regenerate() -> None:
    client = ScriptedModelClient([_response(_plan_payload())])
    audit = InMemoryModelAuditRepository()
    budget = BudgetState(limits=BudgetLimits(max_tool_calls=3))
    with pytest.raises(ModelBudgetError):
        asyncio.run(_planner(client, audit).plan(run_id="run_001", state=_state(budget=budget), validator=_validator()))
    assert client.call_count == 1
    assert audit.attempts[0].error_code is ErrorCode.BUDGET_EXCEEDED


def test_non_schema_model_error_is_audited_without_regeneration() -> None:
    client = ScriptedModelClient([ModelTimeoutError("model provider request timed out")])
    audit = InMemoryModelAuditRepository()
    with pytest.raises(ModelTimeoutError):
        asyncio.run(_planner(client, audit).plan(run_id="run_001", state=_state(), validator=_validator()))
    assert client.call_count == 1
    assert audit.attempts[0].error_code is ErrorCode.LLM_TIMEOUT
