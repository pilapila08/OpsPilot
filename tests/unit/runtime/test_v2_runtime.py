import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from opspilot.agent.schemas import BudgetLimits
from opspilot.agent.state import AgentStatus
from opspilot.diagnosis.v2 import V2Assessment
from opspilot.evidence.models import Claim, Evidence, MissingEvidence, Verification
from opspilot.errors import ErrorCode
from opspilot.execution.repository import InMemoryExecutionRepository
from opspilot.llm.audit import InMemoryModelAuditRepository
from opspilot.llm.client import ScriptedModelClient, ScriptedStep
from opspilot.llm.models import ModelUsage, ScriptedModelResponse, StructuredModelConfig
from opspilot.llm.errors import ModelExternalError, ModelSchemaError
from opspilot.llm.prompts import load_prompt
from opspilot.planning.v2_planner import V2Planner
from opspilot.routing.v2 import IntentV2, V2IntentRouter
from opspilot.runtime.v2 import ObservationRuntimeV2, V2DiagnosisRequest
from opspilot.storage.planning_rounds import InMemoryPlanningRoundRepository
from opspilot.storage.runtime import InMemoryRuntimeRepository
from opspilot.tools import ToolDefinition, ToolRegistry, ToolRiskLevel
from opspilot.tools.errors import ToolExecutionError
from opspilot.tools.kubernetes.models import PodStatusInput, PodStatusOutput

ROOT = Path(__file__).resolve().parents[3]


class Ids:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def new(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"{kind}_{self.counts[kind]:03d}"


class FakeVerifier:
    def __init__(self, foreign: bool = False) -> None:
        self.foreign = foreign
        self.calls = 0

    def verify(
        self, *, intent: IntentV2, evidence: tuple[Evidence, ...],
        force_partial: bool,
    ) -> V2Assessment:
        del intent, force_partial
        self.calls += 1
        cited = "ev_foreign" if self.foreign else evidence[0].evidence_id
        return V2Assessment(
            status="PARTIAL", root_cause="The exact cause remains unverified.",
            recommendation="Collect an additional independent signal.",
            confidence=Decimal("0.3"),
            claim=Claim(
                claim_id="claim_restart", text="A restart was observed.",
                evidence_ids=(cited,), inference_confidence=0.4,
            ),
            verification=Verification(
                claim_id="claim_restart", supported=False,
                verification_confidence=0.3,
                checked_evidence_ids=(cited,),
                missing_evidence=(MissingEvidence(
                    requirement="corroboration", reason="Insufficient fixture signals.",
                ),),
                rationale="Fake verifier deliberately remains Partial.",
            ),
        )


def _response(payload: dict[str, object]) -> ScriptedModelResponse:
    return ScriptedModelResponse(payload=cast(dict[str, JsonValue], payload))


def _router() -> ScriptedModelResponse:
    return _response({
        "intent": "diagnose", "domain": "kubernetes",
        "fault_family": "oom_killed", "target_kind": "deployment", "resource": "api",
    })


def _decision(
    round_no: int, action: str, calls: list[dict[str, object]],
    evidence: list[str] | None = None,
) -> ScriptedModelResponse:
    return _response({
        "schema_version": 2, "round_no": round_no, "action": action,
        "based_on_evidence_ids": evidence or [], "calls": calls,
    })


def _call(call_id: str, *, pod_name: str | None = None) -> dict[str, object]:
    arguments: dict[str, object] = {"namespace": "opspilot-fixtures"}
    if pod_name is None:
        arguments["workload_name"] = "api"
    else:
        arguments["pod_name"] = pod_name
    return {
        "call_id": call_id, "tool": "k8s.get_pod_status",
        "arguments": arguments, "reason": "Read Pod status",
    }


def _registry(*, healthy: bool, fail_explicit: bool = False) -> ToolRegistry:
    branch = "healthy" if healthy else "oom"
    path = ROOT / f"fixtures/cases/restart-branches-v2/responses/{branch}/status.json"
    data = json.loads(path.read_text(encoding="utf-8"))["data"]
    output = PodStatusOutput.model_validate_json(json.dumps(data), strict=True)
    if healthy:
        output = output.model_copy(update={"conditions": ()})

    async def read(target: PodStatusInput) -> PodStatusOutput:
        if fail_explicit and target.pod_name is not None:
            raise ToolExecutionError("scoped status temporarily unavailable")
        return output

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="k8s.get_pod_status", description="Read scoped status",
        risk_level=ToolRiskLevel.READ_ONLY,
        input_model=PodStatusInput, output_model=PodStatusOutput,
        handler=read, source="kubernetes", timeout_seconds=1,
    ))
    return registry


def _runtime(
    steps: list[ScriptedStep], *, healthy: bool = False,
    verifier: FakeVerifier | None = None,
    limits: BudgetLimits | None = None,
    max_semantic_retries: int = 1,
    fail_explicit: bool = False,
    storage: InMemoryRuntimeRepository | None = None,
    audit: InMemoryModelAuditRepository | None = None,
) -> tuple[ObservationRuntimeV2, ScriptedModelClient, InMemoryExecutionRepository, InMemoryPlanningRoundRepository, FakeVerifier]:
    client = ScriptedModelClient(steps)
    audit = audit or InMemoryModelAuditRepository()
    storage = storage or InMemoryRuntimeRepository()
    tools = InMemoryExecutionRepository()
    rounds = InMemoryPlanningRoundRepository()
    chosen_verifier = verifier or FakeVerifier()
    model_config = StructuredModelConfig(provider="scripted", model="fake")
    registry = _registry(healthy=healthy, fail_explicit=fail_explicit)
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
            max_semantic_retries=max_semantic_retries,
        ),
        verifier=chosen_verifier,
        task_repository=storage, run_repository=storage,
        tool_repository=tools, round_repository=rounds,
        result_repository=storage,
        registry_factory=lambda _: registry,
        allowed_resources=lambda _: frozenset({"api", "api-001"}),
        budget_limits=limits, id_factory=Ids().new,
    )
    return runtime, client, tools, rounds, chosen_verifier


def _request() -> V2DiagnosisRequest:
    return V2DiagnosisRequest(
        query="Why is api restarting?", namespace="opspilot-fixtures", mode="live",
    )


def test_v2_zero_evidence_fails_without_verifier() -> None:
    runtime, client, tools, rounds, verifier = _runtime([
        _router(), _decision(1, "continue", [_call("call_status")]),
    ], healthy=True)
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.FAILED
    assert output.error is not None and output.error.code is ErrorCode.SCHEMA_VALIDATION
    assert output.diagnosis is None
    assert len(tools.attempts) == len(rounds.rounds) == 1
    assert verifier.calls == 0
    client.assert_exhausted()


def test_v2_repeated_observation_stops_with_partial() -> None:
    runtime, client, tools, rounds, verifier = _runtime([
        _router(),
        _decision(1, "continue", [_call("call_status")]),
        _decision(2, "continue", [_call("call_explicit_pod", pod_name="api-001")], ["ev_001"]),
    ])
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.PARTIAL, output.error
    assert output.diagnosis is not None
    assert len(rounds.rounds) == 2
    assert len(tools.attempts) == 2
    assert len(tools.evidence) == 4
    assert verifier.calls == 1
    client.assert_exhausted()


def test_v2_entire_round_rejected_before_any_tool_when_budget_insufficient() -> None:
    storage = InMemoryRuntimeRepository()
    runtime, client, tools, rounds, verifier = _runtime([
        _router(), _decision(1, "continue", [
            _call("call_one"), _call("call_two", pod_name="api-001"),
        ]),
    ], limits=BudgetLimits(max_steps=1), max_semantic_retries=0, storage=storage)
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.BUDGET_EXCEEDED
    assert output.error is not None and output.error.code is ErrorCode.BUDGET_EXCEEDED
    assert output.diagnosis is not None and output.diagnosis.status == "PARTIAL"
    assert output.diagnosis.schema_version == 3
    assert output.diagnosis.claims_payload == ()
    assert output.diagnosis.verification_payload["kind"] == "budget_stop"
    assert output.diagnosis.verification_payload["checked_evidence_ids"] == []
    assert not tools.attempts and not rounds.rounds
    assert verifier.calls == 0
    assert client.call_count == 2
    (stop,) = storage.budget_stops_for_trace(output.trace_id)
    assert (stop.reason.dimension, stop.reason.phase, stop.reason.kind) == (
        "steps", "plan.admission", "projected",
    )
    assert (stop.reason.step_no, stop.round_no, stop.reason.requested) == (1, 1, Decimal(2))
    assert stop.reason.budget.steps_used == 0
    client.assert_exhausted()


def test_v2_foreign_verifier_evidence_is_policy_rejected() -> None:
    verifier = FakeVerifier(foreign=True)
    runtime, client, tools, rounds, _ = _runtime([
        _router(), _decision(1, "continue", [_call("call_status")]),
        _decision(2, "finish", [], ["ev_001"]),
    ], verifier=verifier)
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.POLICY_REJECTED
    assert output.error is not None and output.error.code is ErrorCode.POLICY_REJECTED
    assert output.diagnosis is None
    assert len(tools.evidence) == 2
    assert len(rounds.rounds) == 2
    assert verifier.calls == 1
    client.assert_exhausted()


def test_v2_failed_second_tool_keeps_prior_evidence_and_attempt() -> None:
    runtime, client, tools, rounds, verifier = _runtime([
        _router(), _decision(1, "continue", [_call("call_status")]),
        _decision(2, "continue", [_call("call_explicit_pod", pod_name="api-001")], ["ev_001"]),
    ], fail_explicit=True)
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.PARTIAL
    assert output.error is not None and output.error.code is ErrorCode.TOOL_EXECUTION_FAILED
    assert output.diagnosis is not None
    assert [item.response.success for item in tools.attempts] == [True, False]
    assert len(tools.evidence) == 2
    assert len(rounds.rounds) == 2
    assert verifier.calls == 1
    client.assert_exhausted()


def test_v2_budget_exhaustion_after_evidence_persists_partial_result() -> None:
    storage = InMemoryRuntimeRepository()
    runtime, client, tools, rounds, verifier = _runtime([
        _router(), _decision(1, "continue", [_call("call_status")]),
    ], limits=BudgetLimits(max_steps=1), max_semantic_retries=0, storage=storage)
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.BUDGET_EXCEEDED
    assert output.error is not None and output.error.code is ErrorCode.BUDGET_EXCEEDED
    assert output.diagnosis is not None and output.diagnosis.status == "PARTIAL"
    assert output.diagnosis.schema_version == 3
    assert output.diagnosis.claims_payload == ()
    assert output.diagnosis.verification_payload["kind"] == "budget_stop"
    checked = output.diagnosis.verification_payload["checked_evidence_ids"]
    assert isinstance(checked, list) and len(checked) == len(tools.evidence)
    assert len(tools.attempts) == 1
    assert len(rounds.rounds) == 1
    assert verifier.calls == 0
    assert client.call_count == 2  # second Planner is not called after step exhaustion
    (stop,) = storage.budget_stops_for_trace(output.trace_id)
    assert (stop.reason.dimension, stop.reason.phase, stop.reason.step_no, stop.round_no) == (
        "steps", "planner.before", 2, 2,
    )
    assert stop.reason.budget.steps_used == 1
    assert stop.reason.budget.tool_calls_used == 1
    client.assert_exhausted()


def test_v2_failed_router_attempt_retains_usage_and_retry_stop() -> None:
    storage = InMemoryRuntimeRepository()
    audit = InMemoryModelAuditRepository()
    failed = ModelSchemaError(
        "invalid structured response",
        usage=ModelUsage(input_tokens=12, output_tokens=8, total_tokens=20,
                         cost_usd=Decimal("0.000200")),
    )
    runtime, client, tools, rounds, verifier = _runtime(
        [failed], limits=BudgetLimits(max_retries=0), storage=storage, audit=audit,
    )
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.BUDGET_EXCEEDED
    assert output.diagnosis is not None and output.diagnosis.schema_version == 3
    assert output.state.budget.tokens_used == 20
    assert output.state.budget.cost_usd == Decimal("0.0002")
    assert client.call_count == 1
    assert len(audit.attempts) == 1
    assert audit.attempts[0].error_code is ErrorCode.SCHEMA_VALIDATION
    assert audit.attempts[0].input_tokens == 12
    assert not tools.attempts and not rounds.rounds and verifier.calls == 0
    (stop,) = storage.budget_stops_for_trace(output.trace_id)
    assert (stop.reason.dimension, stop.reason.phase, stop.reason.step_no) == (
        "retries", "router.retry", 1,
    )


def test_v2_external_model_failure_keeps_external_error_and_usage() -> None:
    storage = InMemoryRuntimeRepository()
    failed = ModelExternalError(
        "provider unavailable",
        usage=ModelUsage(input_tokens=7, output_tokens=3, total_tokens=10,
                         cost_usd=Decimal("0.000100")),
    )
    runtime, client, tools, rounds, verifier = _runtime([failed], storage=storage)
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.FAILED
    assert output.error is not None and output.error.code is ErrorCode.EXTERNAL_SERVICE_ERROR
    assert output.state.budget.tokens_used == 10
    assert output.state.budget.cost_usd == Decimal("0.0001")
    assert output.diagnosis is None
    assert storage.budget_stops_for_trace(output.trace_id) == ()
    assert client.call_count == 1
    assert not tools.attempts and not rounds.rounds and verifier.calls == 0


def test_v2_unknown_tool_is_policy_terminal_without_execution() -> None:
    bad_call = _call("call_bad")
    bad_call["tool"] = "k8s.delete_pod"
    runtime, client, tools, rounds, verifier = _runtime([
        _router(), _decision(1, "continue", [bad_call]),
    ], max_semantic_retries=0)
    output = asyncio.run(runtime.run(_request()))
    assert output.status is AgentStatus.POLICY_REJECTED
    assert output.error is not None and output.error.code is ErrorCode.TOOL_NOT_FOUND
    assert not tools.attempts and not rounds.rounds
    assert verifier.calls == 0
    client.assert_exhausted()
