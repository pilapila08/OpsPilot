from pathlib import Path
from typing import cast

import pytest

from opspilot.cases import LoadedCase, load_case
from opspilot.evidence import Evidence

PROJECT_ROOT = Path(__file__).parents[3]
CASE_FILE = (
    PROJECT_ROOT / "fixtures" / "cases" / "crashloop-liveness-v1" / "case.json"
)


@pytest.fixture(scope="module")
def crashloop_case() -> LoadedCase:
    return load_case(CASE_FILE)


def _deployment_container(document: dict[str, object]) -> dict[str, object]:
    spec = cast(dict[str, object], document["spec"])
    template = cast(dict[str, object], spec["template"])
    pod_spec = cast(dict[str, object], template["spec"])
    containers = cast(list[dict[str, object]], pod_spec["containers"])
    return containers[0]


def _attributes(evidence: Evidence) -> dict[str, object]:
    return {attribute.key: attribute.value for attribute in evidence.attributes}


def test_case_is_an_ordered_offline_replay(crashloop_case: LoadedCase) -> None:
    definition = crashloop_case.definition

    assert [step.invocation.tool for step in definition.replay_steps] == [
        "k8s.get_pod_status",
        "k8s.get_pod_events",
        "k8s.get_previous_logs",
        "k8s.get_deployment",
        "k8s.get_pod_status",
    ]
    assert [
        response.metadata.call_id for response in crashloop_case.responses
    ] == [step.invocation.call_id for step in definition.replay_steps]
    assert all(response.success for response in crashloop_case.responses)


def test_broken_probe_restarts_before_startup_finishes(
    crashloop_case: LoadedCase,
) -> None:
    definition = crashloop_case.definition
    manifest = crashloop_case.read_json(definition.broken_manifest)
    container = _deployment_container(manifest)
    liveness = cast(dict[str, object], container["livenessProbe"])

    initial_delay = cast(int, liveness["initialDelaySeconds"])
    period = cast(int, liveness["periodSeconds"])
    failure_threshold = cast(int, liveness["failureThreshold"])
    restart_after = initial_delay + period * (failure_threshold - 1)

    assert restart_after == 20
    assert restart_after < definition.startup_duration_seconds == 40
    assert "startupProbe" not in container


def test_ground_truth_covers_every_required_evidence(
    crashloop_case: LoadedCase,
) -> None:
    definition = crashloop_case.definition
    evidence_by_id = {item.evidence_id: item for item in definition.evidence}
    evidence_by_signal = {
        requirement.signal: evidence_by_id[requirement.evidence_id]
        for requirement in definition.evidence_requirements
    }

    assert set(evidence_by_signal) == {
        "restart_count",
        "liveness_failure",
        "startup_duration",
        "probe_configuration",
    }
    assert _attributes(evidence_by_signal["restart_count"])["restart_count"] == 5
    assert (
        _attributes(evidence_by_signal["liveness_failure"])["event_reason"]
        == "Unhealthy"
    )
    assert (
        _attributes(evidence_by_signal["startup_duration"])[
            "startup_duration_seconds"
        ]
        == 40
    )
    assert (
        _attributes(evidence_by_signal["probe_configuration"])[
            "initial_delay_seconds"
        ]
        == 10
    )
    assert definition.expected_diagnosis.verification.supported is True


def test_fixed_probe_allows_startup_and_snapshot_is_healthy(
    crashloop_case: LoadedCase,
) -> None:
    definition = crashloop_case.definition
    manifest = crashloop_case.read_json(definition.fixed_manifest)
    container = _deployment_container(manifest)
    startup_probe = cast(dict[str, object], container["startupProbe"])
    startup_window = cast(int, startup_probe["periodSeconds"]) * cast(
        int,
        startup_probe["failureThreshold"],
    )

    assert startup_window == 50
    assert startup_window > definition.startup_duration_seconds

    response = crashloop_case.response_for("call_recovery_status")
    assert response.data is not None
    status = cast(dict[str, object], response.data["status"])
    container_statuses = cast(list[dict[str, object]], status["containerStatuses"])
    recovered_container = container_statuses[0]

    assert status["phase"] == definition.recovery.phase
    assert recovered_container["ready"] is definition.recovery.ready
    assert (
        cast(int, recovered_container["restartCount"])
        <= definition.recovery.max_restart_count
    )
