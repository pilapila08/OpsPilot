import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import JsonValue

from opspilot.errors import ErrorCode
from opspilot.cases import load_case
from opspilot.runtime import (
    DiagnosisRequest, ReplayConfigurationError, ReplayRegistryFactory,
    ReplayToolRegistry,
)
from opspilot.tools import ToolInvocation

CASE_FILE = Path(__file__).resolve().parents[3] / "fixtures/cases/crashloop-liveness-v1/case.json"
REQUEST = DiagnosisRequest(
    query="Why is slow-start-api restarting?", namespace="opspilot-fixtures",
    mode="replay", case_id="crashloop_liveness_v1",
)


def _calls() -> tuple[ToolInvocation, ...]:
    pod: dict[str, JsonValue] = {"namespace": "opspilot-fixtures", "workload_name": "slow-start-api"}
    return (
        ToolInvocation(call_id="call_pod_status", tool="k8s.get_pod_status", arguments=pod),
        ToolInvocation(call_id="call_pod_events", tool="k8s.get_pod_events", arguments=pod),
        ToolInvocation(call_id="call_previous_logs", tool="k8s.get_previous_logs", arguments={**pod, "container_name": "slow-start-api"}),
        ToolInvocation(call_id="call_deployment", tool="k8s.get_deployment", arguments={"namespace": "opspilot-fixtures", "deployment_name": "slow-start-api"}),
    )


def _registry() -> ReplayToolRegistry:
    factory = ReplayRegistryFactory({"crashloop_liveness_v1": CASE_FILE})
    registry = factory(REQUEST)
    assert isinstance(registry, ReplayToolRegistry)
    return registry


def test_replay_runs_four_fault_steps_through_registered_handlers() -> None:
    registry = _registry()
    responses = [asyncio.run(registry.invoke(call)) for call in _calls()]
    assert all(response.success for response in responses)
    assert registry.consumed_calls == 4
    assert {response.metadata.tool_name for response in responses} == {
        "k8s.get_pod_status", "k8s.get_pod_events",
        "k8s.get_previous_logs", "k8s.get_deployment",
    }
    assert responses[0].data is not None
    status_containers = responses[0].data["containers"]
    assert isinstance(status_containers, list)
    assert isinstance(status_containers[0], dict)
    assert status_containers[0]["restart_count"] == 5
    assert responses[3].data is not None
    deployment_containers = responses[3].data["containers"]
    assert isinstance(deployment_containers, list)
    assert isinstance(deployment_containers[0], dict)
    liveness = deployment_containers[0]["liveness_probe"]
    assert isinstance(liveness, dict)
    assert liveness["initial_delay_seconds"] == 10


@pytest.mark.parametrize("call", [
    _calls()[1],
    ToolInvocation(call_id="call_pod_status", tool="k8s.get_pod_events", arguments=_calls()[0].arguments),
    ToolInvocation(call_id="call_pod_status", tool="k8s.get_pod_status", arguments={"namespace": "opspilot-fixtures", "workload_name": "other-api"}),
    ToolInvocation(call_id="call_recovery_status", tool="k8s.get_pod_status", arguments=_calls()[0].arguments),
])
def test_replay_mismatch_is_policy_rejected_without_advancing(call: ToolInvocation) -> None:
    registry = _registry()
    response = asyncio.run(registry.invoke(call))
    assert not response.success
    assert response.error is not None and response.error.code is ErrorCode.POLICY_REJECTED
    assert registry.consumed_calls == 0


def test_fresh_factory_call_does_not_share_replay_position() -> None:
    first = _registry()
    assert asyncio.run(first.invoke(_calls()[0])).success
    second = _registry()
    assert second.consumed_calls == 0
    assert asyncio.run(second.invoke(_calls()[0])).success


@pytest.mark.parametrize("key,value", [
    ("namespace", "other-team"),
    ("pod", "slow-start-api-other-pod"),
])
def test_corrupt_case_invocation_identity_is_rejected(key: str, value: str) -> None:
    case = load_case(CASE_FILE)
    original = case.definition.replay_steps[0]
    wrong_call = original.invocation.model_copy(update={
        "arguments": {**original.invocation.arguments, key: value},
    })
    wrong_step = original.model_copy(update={"invocation": wrong_call})
    definition = case.definition.model_copy(update={
        "replay_steps": (wrong_step, *case.definition.replay_steps[1:]),
    })
    with pytest.raises(ReplayConfigurationError):
        ReplayToolRegistry(replace(case, definition=definition))
