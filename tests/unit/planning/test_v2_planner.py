import asyncio
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from opspilot.agent.schemas import BudgetLimits, BudgetState
from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes import KubernetesReader
from opspilot.llm.audit import InMemoryModelAuditRepository
from opspilot.llm.client import ScriptedModelClient
from opspilot.llm.models import ScriptedModelResponse, StructuredModelConfig
from opspilot.llm.prompts import load_prompt
from opspilot.planning.v2 import ObservationSummaryV2, V2PlanValidator
from opspilot.planning.v2_planner import V2Planner, V2PlannerOutcome, V2PlanningError
from opspilot.routing.v2 import IntentV2, V2Target
from opspilot.tools.kubernetes import build_kubernetes_registry

PROMPT = Path(__file__).resolve().parents[3] / "prompts/planner/v2.md"


def _intent() -> IntentV2:
    return IntentV2(
        domain="kubernetes", fault_family="oom_killed",
        target=V2Target(namespace="opspilot-fixtures", kind="deployment", resource="api"),
    )


def _payload(tool: str = "k8s.get_pod_status") -> dict[str, object]:
    return {
        "schema_version": 2, "round_no": 1, "action": "continue",
        "based_on_evidence_ids": [],
        "calls": [{
            "call_id": "call_status", "tool": tool,
            "arguments": {"namespace": "opspilot-fixtures", "workload_name": "api"},
            "reason": "Observe actual Pod state",
        }],
    }


def _planner(steps: list[ScriptedModelResponse]) -> tuple[V2Planner, ScriptedModelClient, InMemoryModelAuditRepository]:
    client = ScriptedModelClient(steps)
    audit = InMemoryModelAuditRepository()
    return V2Planner(
        client=client, audit_repository=audit,
        prompt=load_prompt(PROMPT, component="planner", version="v2"),
        model_config=StructuredModelConfig(provider="scripted", model="fake"),
    ), client, audit


async def _run(planner: V2Planner, budget: BudgetState | None = None) -> V2PlannerOutcome:
    return await planner.plan(
        run_id="run_001", intent=_intent(),
        observation=ObservationSummaryV2.from_persisted("trace_001", ()),
        round_no=1, budget=budget or BudgetState(),
        validator=V2PlanValidator(build_kubernetes_registry(cast(KubernetesReader, object()))),
        used_call_ids=frozenset(), used_requests=frozenset(),
        allowed_resources=frozenset({"api"}),
    )


def _response(payload: dict[str, object], *, input_tokens: int = 0, output_tokens: int = 0) -> ScriptedModelResponse:
    return ScriptedModelResponse(
        payload=cast(dict[str, JsonValue], payload),
        input_tokens=input_tokens, output_tokens=output_tokens,
    )


def test_v2_planner_audits_admitted_decision_without_raw_output() -> None:
    planner, client, audit = _planner([_response(_payload(), input_tokens=11, output_tokens=7)])
    outcome = asyncio.run(_run(planner))
    assert outcome.admitted.decision.calls[0].tool == "k8s.get_pod_status"
    assert outcome.budget.tokens_used == 18
    assert outcome.prompt_version_id.startswith("prompt_planner_v2_")
    assert len(outcome.llm_call_ids) == 1
    assert audit.attempts[0].response_payload is not None
    assert "arguments" not in str(audit.attempts[0].response_payload)
    assert client.output_models[0].__name__ == "PlanDecisionV2"
    assert "raw_result_ref" not in client.requests[0].messages[1].content


def test_v2_planner_schema_and_policy_retries_are_bounded_and_audited() -> None:
    planner, client, audit = _planner([
        _response({"unexpected": True}, input_tokens=2, output_tokens=1),
        _response(_payload("k8s.delete_pod"), input_tokens=2, output_tokens=1),
        _response(_payload(), input_tokens=2, output_tokens=1),
    ])
    outcome = asyncio.run(_run(planner, BudgetState(limits=BudgetLimits(max_retries=2))))
    assert outcome.budget.retries_used == 2
    assert outcome.budget.tokens_used == 9
    assert [item.error_code for item in audit.attempts] == [
        ErrorCode.SCHEMA_VALIDATION, ErrorCode.TOOL_NOT_FOUND, None,
    ]
    assert client.call_count == 3


def test_v2_planner_budget_exhaustion_stops_before_extra_attempt() -> None:
    planner, client, audit = _planner([
        _response({"wrong": True}, input_tokens=1, output_tokens=1),
        _response(_payload()),
    ])
    with pytest.raises(V2PlanningError) as exc:
        asyncio.run(_run(planner, BudgetState(limits=BudgetLimits(max_retries=0))))
    assert exc.value.error.code is ErrorCode.BUDGET_EXCEEDED
    assert exc.value.budget.tokens_used == 2
    assert len(audit.attempts) == 1
    assert client.call_count == 1
