import json
from pathlib import Path
import shutil
from typing import Any, cast

import pytest
from pydantic import ValidationError

from opspilot.cases import (
    CaseDefinitionV2,
    LoadedCaseV2,
    ReplayMismatchV2,
    load_case_versioned,
    load_case_v2,
)
from opspilot.tools import ToolInvocation
from opspilot.tools.kubernetes.models import (
    DeploymentOutput,
    PodEventsOutput,
    PodStatusOutput,
)

ROOT = Path(__file__).resolve().parents[3]
V1_CASE = ROOT / "fixtures/cases/crashloop-liveness-v1/case.json"
V2_CASE = ROOT / "fixtures/cases/restart-branches-v2/case.json"


def _payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(V2_CASE.read_text(encoding="utf-8")))


def test_version_dispatch_preserves_v1_and_loads_v2_branches() -> None:
    assert load_case_versioned(V1_CASE).definition.schema_version == 1
    loaded = load_case_versioned(V2_CASE)
    assert isinstance(loaded, LoadedCaseV2)
    assert loaded.definition.schema_version == 2
    assert [item.definition.branch_id for item in loaded.branches] == [
        "oom_branch", "probe_branch", "healthy_branch"
    ]
    oom, probe, healthy = loaded.branches
    assert oom.definition.steps[0].invocation == probe.definition.steps[0].invocation
    assert oom.definition.steps[1].invocation.tool == "k8s.get_deployment"
    assert probe.definition.steps[1].invocation.tool == "k8s.get_pod_events"
    assert oom.definition.expected_diagnosis.status == "COMPLETED"
    assert probe.definition.expected_diagnosis.status == "PARTIAL"
    assert probe.definition.required_signals[-1].evidence_id is None
    assert healthy.definition.expected_diagnosis.verification.contradictions


@pytest.mark.parametrize("branch_id", ["oom_branch", "probe_branch"])
def test_replay_session_fails_closed_and_checks_complete(branch_id: str) -> None:
    loaded = load_case_v2(V2_CASE)
    branch = next(
        item for item in loaded.branches if item.definition.branch_id == branch_id
    )
    session = loaded.open_branch(branch_id)
    second = branch.definition.steps[1].invocation
    with pytest.raises(ReplayMismatchV2, match="does not match"):
        session.next_response(second)
    with pytest.raises(ReplayMismatchV2, match="unconsumed"):
        session.assert_complete()

    first = branch.definition.steps[0].invocation
    altered = first.model_copy(update={"arguments": {
        "namespace": "prod", "workload_name": "api"
    }})
    with pytest.raises(ReplayMismatchV2, match="does not match"):
        session.next_response(altered)
    first_response = session.next_response(first)
    second_response = session.next_response(second)
    assert first_response.metadata.call_id == first.call_id
    assert second_response.metadata.tool_name == second.tool
    session.assert_complete()
    with pytest.raises(ReplayMismatchV2, match="no remaining"):
        session.next_response(second)
    with pytest.raises(ReplayMismatchV2, match="not declared"):
        loaded.open_branch("unknown_branch")


def test_v2_response_data_matches_current_registered_kubernetes_shapes() -> None:
    loaded = load_case_v2(V2_CASE)
    oom, probe, healthy = loaded.branches
    assert oom.responses[0].data is not None
    assert oom.responses[1].data is not None
    assert probe.responses[1].data is not None
    pod = PodStatusOutput.model_validate_json(
        json.dumps(oom.responses[0].data), strict=True
    )
    deployment = DeploymentOutput.model_validate_json(
        json.dumps(oom.responses[1].data), strict=True
    )
    events = PodEventsOutput.model_validate_json(
        json.dumps(probe.responses[1].data), strict=True
    )
    assert pod.containers[0].last_reason == "OOMKilled"
    assert deployment.containers[0].resources.limits["memory"] == "128Mi"
    assert events.events[0].reason == "Unhealthy"
    assert healthy.responses[0].data is not None
    ready = PodStatusOutput.model_validate_json(
        json.dumps(healthy.responses[0].data), strict=True
    )
    assert ready.containers[0].restart_count == 0
    assert ready.containers[0].ready is True


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["branches"][0].update({"branch_id": "probe_branch"}),
        lambda data: data["branches"][0]["steps"][1]["invocation"].update(
            {"call_id": "call_status"}
        ),
        lambda data: data["branches"][0]["steps"][0]["invocation"][
            "arguments"
        ].update({"namespace": "prod"}),
        lambda data: data["branches"][0]["steps"][1]["invocation"][
            "arguments"
        ].update({"deployment_name": "other-api"}),
        lambda data: data["branches"][0]["steps"][1]["invocation"].update(
            {"tool": "k8s.delete_deployment"}
        ),
        lambda data: data["branches"][0]["steps"][0].update(
            {"response_file": "../outside.json"}
        ),
        lambda data: data["branches"][0]["required_signals"][0].update(
            {"evidence_id": "ev_unknown"}
        ),
        lambda data: data["branches"][0]["expected_diagnosis"][
            "verification"
        ].update({"supported": False}),
    ],
)
def test_v2_case_rejects_invalid_scope_or_ground_truth(change: Any) -> None:
    payload = _payload()
    change(payload)
    with pytest.raises(ValidationError):
        CaseDefinitionV2.model_validate(payload, strict=True)


def test_v2_loader_rejects_mismatched_response_and_path_escape(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    shutil.copytree(V2_CASE.parent, case_dir)
    response = case_dir / "responses/oom/status.json"
    data = json.loads(response.read_text(encoding="utf-8"))
    data["metadata"]["call_id"] = "call_wrong"
    response.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata"):
        load_case_v2(case_dir / "case.json")

    response.write_text(
        (V2_CASE.parent / "responses/oom/status.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.json"
    outside.write_text(response.read_text(encoding="utf-8"), encoding="utf-8")
    response.unlink()
    try:
        response.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ValueError, match="escapes"):
        load_case_v2(case_dir / "case.json")


def test_version_dispatch_rejects_unknown_and_non_object(tmp_path: Path) -> None:
    case_file = tmp_path / "case.json"
    payloads: tuple[object, ...] = (
        {"schema_version": 3}, {"schema_version": True}, [],
    )
    for payload in payloads:
        case_file.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            load_case_versioned(case_file)


def test_branch_expected_invocation_is_exact_structured_data() -> None:
    loaded = load_case_v2(V2_CASE)
    first = loaded.branches[0].definition.steps[0].invocation
    assert isinstance(first, ToolInvocation)
    assert first.arguments == {
        "namespace": "opspilot-fixtures",
        "workload_name": "api",
    }
