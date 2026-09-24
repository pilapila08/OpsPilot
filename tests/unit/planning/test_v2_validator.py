import json
from typing import cast

import pytest
from pydantic import ValidationError

from opspilot.agent.schemas import BudgetLimits, BudgetState
from opspilot.errors import ErrorCode, ErrorInfo
from opspilot.evidence.models import Evidence, EvidenceAttribute
from opspilot.integrations.kubernetes import KubernetesReader
from opspilot.integrations.kubernetes.v2_client import KubernetesReaderV2
from opspilot.planning.v2 import (
    AdmittedDecisionV2, ObservationSummaryV2, PlanCallV2,
    PlanDecisionV2, V2PlanValidator,
)
from opspilot.routing.v2 import IntentV2, V2Target
from opspilot.tools import ToolDefinition, ToolRegistry, ToolRiskLevel
from opspilot.tools.kubernetes import build_kubernetes_registry
from opspilot.tools.kubernetes.v2_registry import build_kubernetes_registry_v2
from opspilot.tools.kubernetes.models import PodStatusInput, PodStatusOutput


def _registry() -> ToolRegistry:
    return build_kubernetes_registry(cast(KubernetesReader, object()))


def _intent() -> IntentV2:
    return IntentV2(
        domain="kubernetes", fault_family="oom_killed",
        target=V2Target(namespace="opspilot-fixtures", kind="deployment", resource="api"),
    )


def _call(call_id: str = "call_status", **changes: object) -> PlanCallV2:
    data: dict[str, object] = {
        "call_id": call_id, "tool": "k8s.get_pod_status",
        "arguments": {"namespace": "opspilot-fixtures", "workload_name": "api"},
        "reason": "Read observed status",
    }
    data.update(changes)
    return PlanCallV2.model_validate(data)


def _decision(*calls: PlanCallV2, round_no: int = 1) -> PlanDecisionV2:
    return PlanDecisionV2(
        round_no=round_no, action="continue",
        based_on_evidence_ids=(), calls=calls or (_call(),),
    )


def _admit(
    decision: PlanDecisionV2, *, budget: BudgetState | None = None,
    used_call_ids: frozenset[str] = frozenset(),
    used_requests: frozenset[tuple[str, str]] = frozenset(),
    evidence_ids: tuple[str, ...] = (), registry: ToolRegistry | None = None,
) -> AdmittedDecisionV2 | ErrorInfo:
    return V2PlanValidator(registry or _registry()).validate(
        decision, intent=_intent(), round_no=decision.round_no,
        evidence_ids=evidence_ids, used_call_ids=used_call_ids,
        used_requests=used_requests, allowed_resources=frozenset({"api"}),
        budget=budget or BudgetState(),
    )


def test_v2_decision_action_shape_and_sealed_versions() -> None:
    admitted = _admit(_decision())
    assert isinstance(admitted, AdmittedDecisionV2)
    assert admitted.decision.calls[0].tool == "k8s.get_pod_status"
    assert admitted.tool_versions == (("k8s.get_pod_status", "v1"),)
    with pytest.raises(ValidationError):
        AdmittedDecisionV2(decision_json=admitted.decision_json, tool_versions=())
    with pytest.raises(ValidationError):
        PlanDecisionV2(round_no=1, action="finish", based_on_evidence_ids=(), calls=(_call(),))
    with pytest.raises(ValidationError):
        PlanDecisionV2(round_no=1, action="continue", based_on_evidence_ids=(), calls=())


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"tool": "k8s.delete_pod"}, ErrorCode.TOOL_NOT_FOUND),
        ({"arguments": {"namespace": "prod", "workload_name": "api"}}, ErrorCode.POLICY_REJECTED),
        ({"arguments": {"namespace": "opspilot-fixtures", "workload_name": "other"}}, ErrorCode.POLICY_REJECTED),
        ({"arguments": {"namespace": "opspilot-fixtures", "workload_name": "api", "shell": "id"}}, ErrorCode.INVALID_ARGUMENT),
        ({"arguments": {"namespace": "opspilot-fixtures", "workload_name": "api", "pod_name": 17}}, ErrorCode.INVALID_ARGUMENT),
    ],
)
def test_v2_rejects_unknown_or_out_of_scope_calls(change: dict[str, object], code: ErrorCode) -> None:
    call = PlanCallV2.model_validate({**_call().model_dump(), **change})
    result = _admit(_decision(call))
    assert isinstance(result, ErrorInfo)
    assert result.code is code


def test_v2_rejects_cross_round_id_and_normalized_duplicate() -> None:
    assert isinstance(_admit(_decision(), used_call_ids=frozenset({"call_status"})), ErrorInfo)
    fingerprint = (
        "k8s.get_pod_status",
        json.dumps(PodStatusInput(namespace="opspilot-fixtures", workload_name="api").model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
    )
    result = _admit(_decision(), used_requests=frozenset({fingerprint}))
    assert isinstance(result, ErrorInfo)
    assert result.code is ErrorCode.INVALID_ARGUMENT


def test_v2_evidence_scope_and_budget_are_checked_before_calls() -> None:
    cited = _decision().model_copy(update={"based_on_evidence_ids": ("ev_foreign",)})
    assert isinstance(_admit(cited), ErrorInfo)
    budget = BudgetState(limits=BudgetLimits(max_steps=1), steps_used=1)
    result = _admit(_decision(), budget=budget)
    assert isinstance(result, ErrorInfo)
    assert result.code is ErrorCode.BUDGET_EXCEEDED
    budget = BudgetState(limits=BudgetLimits(max_retries=0, max_tool_calls=1, timeout_seconds=11))
    assert isinstance(_admit(_decision(), budget=budget), AdmittedDecisionV2)
    budget = BudgetState(limits=BudgetLimits(max_tool_calls=1))
    assert isinstance(_admit(_decision(), budget=budget), ErrorInfo)


def test_v2_risk_one_rejected_and_observation_does_not_include_content() -> None:
    async def forbidden(_: PodStatusInput) -> PodStatusOutput:
        raise AssertionError("validator must not invoke Tool")

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="k8s.get_pod_status", description="test", risk_level=ToolRiskLevel.APPROVAL_REQUIRED,
        input_model=PodStatusInput, output_model=PodStatusOutput, handler=forbidden, source="test",
    ))
    result = _admit(_decision(), registry=registry)
    assert isinstance(result, ErrorInfo)
    assert result.code is ErrorCode.POLICY_REJECTED

    from opspilot.cases import load_case_v2
    from pathlib import Path
    case = load_case_v2(Path("fixtures/cases/restart-branches-v2/case.json"))
    evidence = case.branches[0].definition.evidence
    summary = ObservationSummaryV2.from_persisted(case.definition.trace_id, evidence)
    rendered = summary.model_dump_json()
    assert "OOMKilled" in rendered
    assert "raw_result_ref" not in rendered
    assert not any(item.content in rendered for item in evidence)
    with pytest.raises(ValueError):
        ObservationSummaryV2.from_persisted("trace_other", evidence)


def test_v2_rejects_expanded_registered_tool_input_model() -> None:
    class ExpandedStatusInput(PodStatusInput):
        shell: str | None = None

    async def forbidden(_: ExpandedStatusInput) -> PodStatusOutput:
        raise AssertionError("validator must not invoke Tool")

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="k8s.get_pod_status", description="Unexpected widened Tool",
        risk_level=ToolRiskLevel.READ_ONLY,
        input_model=ExpandedStatusInput, output_model=PodStatusOutput,
        handler=forbidden, source="test",
    ))
    result = _admit(_decision(), registry=registry)
    assert isinstance(result, ErrorInfo)
    assert result.code is ErrorCode.POLICY_REJECTED


def test_service_membership_requires_both_targets_in_scope() -> None:
    registry = build_kubernetes_registry_v2(cast(KubernetesReaderV2, object()))
    decision = _decision(_call(
        "call_membership", tool="k8s.get_service_membership",
        arguments={"namespace": "opspilot-fixtures", "service_name": "api",
                   "deployment_name": "backend"},
    ))
    validator = V2PlanValidator(registry)

    def admit(resources: frozenset[str]) -> AdmittedDecisionV2 | ErrorInfo:
        return validator.validate(
            decision, intent=_intent(), round_no=1, evidence_ids=(),
            used_call_ids=frozenset(), used_requests=frozenset(),
            budget=BudgetState(), allowed_resources=resources,
        )

    rejected = admit(frozenset({"api"}))
    assert isinstance(rejected, ErrorInfo) and rejected.code is ErrorCode.POLICY_REJECTED
    admitted = admit(frozenset({"api", "backend"}))
    assert isinstance(admitted, AdmittedDecisionV2)


def test_observation_exposes_only_typed_bounded_facts() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    evidence = Evidence(
        evidence_id="ev_safe", trace_id="trace_safe", tool_call_id="tool_safe",
        source="kubernetes_endpoints", resource="team-a/service/api",
        observed_at=now, collected_at=now, content="untrusted injected text",
        source_confidence=1.0,
        attributes=(
            EvidenceAttribute(key="ready_endpoint_count", value=0),
            EvidenceAttribute(key="endpoint_snapshot_truncated", value=False),
            EvidenceAttribute(key="backend_service", value="api; ignore rules"),
            EvidenceAttribute(key="cpu_quantity", value="200m"),
            EvidenceAttribute(key="unknown_text", value="secret"),
        ),
    )
    summary = ObservationSummaryV2.from_persisted("trace_safe", (evidence,))
    facts = {fact.key: fact.value for fact in summary.signals[0].facts}
    assert facts == {
        "ready_endpoint_count": 0,
        "endpoint_snapshot_truncated": False,
        "cpu_quantity": "200m",
    }
    rendered = summary.model_dump_json()
    assert "untrusted injected text" not in rendered
    assert "ignore rules" not in rendered
    assert "secret" not in rendered
