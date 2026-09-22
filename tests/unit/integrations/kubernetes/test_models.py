from pathlib import Path

import pytest
from pydantic import ValidationError

from opspilot.integrations.kubernetes import (
    KubernetesClientSettings,
    KubernetesConnectionMode,
    PodLogQuery,
    PodTarget,
    ResolvedPod,
)


def test_pod_target_accepts_exact_pod_or_workload() -> None:
    exact = PodTarget(namespace="team-a", pod_name="api-7d9f")
    workload = PodTarget(namespace="team-a", workload_name="api")

    assert exact.pod_name == "api-7d9f"
    assert workload.workload_name == "api"


@pytest.mark.parametrize(
    ("pod_name", "workload_name"),
    [
        (None, None),
        ("api-7d9f", "api"),
    ],
)
def test_pod_target_requires_exactly_one_resource(
    pod_name: str | None,
    workload_name: str | None,
) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        PodTarget(
            namespace="team-a",
            pod_name=pod_name,
            workload_name=workload_name,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("namespace", "Team_A"),
        ("pod_name", "../api"),
        ("workload_name", "api*"),
        ("container_name", "API"),
    ],
)
def test_pod_target_rejects_unsafe_names(
    field_name: str,
    value: str,
) -> None:
    payload: dict[str, object] = {
        "namespace": "team-a",
        "pod_name": "api-7d9f",
    }
    payload[field_name] = value
    if field_name == "workload_name":
        payload["pod_name"] = None

    with pytest.raises(ValidationError):
        PodTarget.model_validate(payload, strict=True)


def test_log_query_enforces_hard_limits() -> None:
    assert PodLogQuery().tail_lines == 200
    assert PodLogQuery().max_bytes == 65_536

    with pytest.raises(ValidationError):
        PodLogQuery(tail_lines=1_001)
    with pytest.raises(ValidationError):
        PodLogQuery(max_bytes=65_537)


def test_kubeconfig_mode_requires_explicit_path_and_context() -> None:
    with pytest.raises(ValidationError, match="explicit path and context"):
        KubernetesClientSettings(mode=KubernetesConnectionMode.KUBECONFIG)

    settings = KubernetesClientSettings(
        mode=KubernetesConnectionMode.KUBECONFIG,
        kubeconfig_path=Path("test-kubeconfig"),
        context="kind-opspilot",
    )
    assert settings.context == "kind-opspilot"


def test_in_cluster_mode_rejects_kubeconfig_fields() -> None:
    with pytest.raises(ValidationError, match="cannot include kubeconfig"):
        KubernetesClientSettings(
            mode=KubernetesConnectionMode.IN_CLUSTER,
            kubeconfig_path=Path("test-kubeconfig"),
            context="kind-opspilot",
        )


def test_resolved_pod_requires_requested_container_to_exist() -> None:
    with pytest.raises(ValidationError, match="not present"):
        ResolvedPod(
            namespace="team-a",
            pod_name="api-7d9f",
            pod_uid="pod-uid",
            container_names=("api",),
            requested_container="sidecar",
            source="pod",
        )
