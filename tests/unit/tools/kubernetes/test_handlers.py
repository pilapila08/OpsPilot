import asyncio

import pytest

from opspilot.integrations.kubernetes import (
    JsonObject,
    KubernetesAmbiguousTargetError,
)
from opspilot.tools.kubernetes import (
    DeploymentInput,
    KubernetesToolHandlers,
    PodEventsInput,
    PodLogsInput,
    PodStatusInput,
    PreviousPodLogsInput,
)


def _pod(
    *,
    name: str = "api-pod",
    containers: tuple[str, ...] = ("api",),
) -> JsonObject:
    return {
        "metadata": {
            "namespace": "team-a",
            "name": name,
            "uid": "pod-uid",
        },
        "spec": {
            "containers": [{"name": container} for container in containers],
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "ContainersNotReady",
                    "message": "containers with unready status",
                    "lastTransitionTime": "2026-09-22T01:00:00Z",
                }
            ],
            "containerStatuses": [
                {
                    "name": containers[0],
                    "ready": False,
                    "restartCount": 5,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                        }
                    },
                    "lastState": {
                        "terminated": {
                            "exitCode": 137,
                            "reason": "Error",
                            "startedAt": "2026-09-22T01:00:00Z",
                            "finishedAt": "2026-09-22T01:00:20Z",
                        }
                    },
                }
            ],
        },
    }


def _event(
    name: str,
    *,
    timestamp: str,
    message: str = "Liveness probe failed",
) -> JsonObject:
    return {
        "metadata": {"name": name},
        "type": "Warning",
        "reason": "Unhealthy",
        "message": message,
        "count": 3,
        "firstTimestamp": "2026-09-22T01:00:00Z",
        "lastTimestamp": timestamp,
        "involvedObject": {
            "namespace": "team-a",
            "name": "api-pod",
            "uid": "pod-uid",
        },
    }


def _deployment() -> JsonObject:
    return {
        "metadata": {
            "namespace": "team-a",
            "name": "api",
            "generation": 7,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "api"}},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "api",
                            "image": "example/api:v1",
                            "livenessProbe": {
                                "httpGet": {
                                    "path": "/healthz",
                                    "port": "http",
                                    "scheme": "HTTP",
                                },
                                "initialDelaySeconds": 10,
                                "periodSeconds": 5,
                                "timeoutSeconds": 1,
                                "failureThreshold": 3,
                                "successThreshold": 1,
                            },
                            "resources": {
                                "requests": {
                                    "cpu": "100m",
                                    "memory": "64Mi",
                                },
                                "limits": {
                                    "memory": "128Mi",
                                },
                            },
                            "env": [
                                {
                                    "name": "STARTUP_DELAY_SECONDS",
                                    "value": "40",
                                },
                                {
                                    "name": "DATABASE_PASSWORD",
                                    "value": "do-not-leak",
                                },
                                {
                                    "name": "API_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "api-secrets",
                                            "key": "token",
                                            "optional": False,
                                        }
                                    },
                                },
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {
                                        "fieldRef": {
                                            "fieldPath": "metadata.name",
                                        }
                                    },
                                },
                                {
                                    "name": "LOG_LEVEL",
                                    "valueFrom": {
                                        "configMapKeyRef": {
                                            "name": "api-config",
                                            "key": "log-level",
                                            "optional": True,
                                        }
                                    },
                                },
                            ],
                        }
                    ]
                }
            },
        },
        "status": {
            "replicas": 1,
            "readyReplicas": 0,
            "unavailableReplicas": 1,
        },
    }


class FakeReader:
    def __init__(
        self,
        *,
        pod: JsonObject | None = None,
        events: tuple[JsonObject, ...] = (),
        log_content: str = "",
        deployment: JsonObject | None = None,
    ) -> None:
        self.pod = pod or _pod()
        self.events = events
        self.log_content = log_content
        self.deployment = deployment or _deployment()
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def read_pod(self, *, namespace: str, pod_name: str) -> JsonObject:
        self.calls.append(
            ("read_pod", {"namespace": namespace, "pod_name": pod_name})
        )
        return self.pod

    async def list_pods(
        self,
        *,
        namespace: str,
        label_selector: str,
    ) -> tuple[JsonObject, ...]:
        self.calls.append(
            (
                "list_pods",
                {
                    "namespace": namespace,
                    "label_selector": label_selector,
                },
            )
        )
        return (self.pod,)

    async def list_events(
        self,
        *,
        namespace: str,
        field_selector: str,
        limit: int,
    ) -> tuple[JsonObject, ...]:
        self.calls.append(
            (
                "list_events",
                {
                    "namespace": namespace,
                    "field_selector": field_selector,
                    "limit": limit,
                },
            )
        )
        return self.events

    async def read_pod_log(
        self,
        *,
        namespace: str,
        pod_name: str,
        container_name: str | None,
        previous: bool,
        tail_lines: int,
        since_seconds: int | None,
        max_bytes: int,
    ) -> str:
        self.calls.append(
            (
                "read_pod_log",
                {
                    "namespace": namespace,
                    "pod_name": pod_name,
                    "container_name": container_name,
                    "previous": previous,
                    "tail_lines": tail_lines,
                    "since_seconds": since_seconds,
                    "max_bytes": max_bytes,
                },
            )
        )
        return self.log_content

    async def read_deployment(
        self,
        *,
        namespace: str,
        deployment_name: str,
    ) -> JsonObject:
        self.calls.append(
            (
                "read_deployment",
                {
                    "namespace": namespace,
                    "deployment_name": deployment_name,
                },
            )
        )
        return self.deployment


def test_pod_status_normalizes_crashloop_facts_without_duplicate_read() -> None:
    reader = FakeReader()
    output = asyncio.run(
        KubernetesToolHandlers(reader).get_pod_status(
            PodStatusInput(namespace="team-a", pod_name="api-pod")
        )
    )

    assert output.phase == "Running"
    assert output.containers[0].state == "waiting"
    assert output.containers[0].reason == "CrashLoopBackOff"
    assert output.containers[0].restart_count == 5
    assert output.containers[0].last_exit_code == 137
    assert [call[0] for call in reader.calls] == ["read_pod"]


def test_events_are_uid_scoped_limited_sorted_and_message_bounded() -> None:
    events = tuple(
        _event(
            f"event-{index:03d}",
            timestamp=(
                "2026-09-22T02:00:00Z"
                if index == 1
                else "2026-09-22T01:00:00Z"
            ),
            message="x" * 3_000,
        )
        for index in range(101)
    )
    reader = FakeReader(events=events)

    output = asyncio.run(
        KubernetesToolHandlers(reader).get_pod_events(
            PodEventsInput(namespace="team-a", pod_name="api-pod")
        )
    )

    assert len(output.events) == 100
    assert output.events[0].name == "event-001"
    assert len(output.events[0].message) == 2_048
    assert reader.calls[-1] == (
        "list_events",
        {
            "namespace": "team-a",
            "field_selector": (
                "involvedObject.uid=pod-uid,"
                "involvedObject.name=api-pod"
            ),
            "limit": 100,
        },
    )


def test_events_allow_empty_result() -> None:
    output = asyncio.run(
        KubernetesToolHandlers(FakeReader()).get_pod_events(
            PodEventsInput(namespace="team-a", pod_name="api-pod")
        )
    )

    assert output.events == ()


@pytest.mark.parametrize(
    "content",
    [
        "a" * 1_025,
        "你" * 400,
    ],
)
def test_current_logs_truncate_safely_by_utf8_bytes(content: str) -> None:
    reader = FakeReader(log_content=content)

    output = asyncio.run(
        KubernetesToolHandlers(reader).get_pod_logs(
            PodLogsInput(
                namespace="team-a",
                pod_name="api-pod",
                max_bytes=1_024,
            )
        )
    )

    assert output.previous is False
    assert output.truncated is True
    assert output.byte_count == len(output.content.encode("utf-8"))
    assert output.byte_count <= 1_024
    output.content.encode("utf-8").decode("utf-8")


def test_previous_logs_force_previous_and_forward_bounds() -> None:
    reader = FakeReader(log_content="previous output")

    output = asyncio.run(
        KubernetesToolHandlers(reader).get_previous_logs(
            PreviousPodLogsInput(
                namespace="team-a",
                pod_name="api-pod",
                container_name="api",
                tail_lines=50,
                since_seconds=300,
                max_bytes=2_048,
            )
        )
    )

    assert output.previous is True
    assert reader.calls[-1] == (
        "read_pod_log",
        {
            "namespace": "team-a",
            "pod_name": "api-pod",
            "container_name": "api",
            "previous": True,
            "tail_lines": 50,
            "since_seconds": 300,
            "max_bytes": 2_048,
        },
    )


def test_multi_container_logs_require_explicit_container() -> None:
    reader = FakeReader(pod=_pod(containers=("api", "sidecar")))

    with pytest.raises(KubernetesAmbiguousTargetError, match="Container name"):
        asyncio.run(
            KubernetesToolHandlers(reader).get_pod_logs(
                PodLogsInput(namespace="team-a", pod_name="api-pod")
            )
        )

    assert all(call[0] != "read_pod_log" for call in reader.calls)


def test_deployment_normalizes_probes_resources_and_redacts_environment() -> None:
    reader = FakeReader()

    output = asyncio.run(
        KubernetesToolHandlers(reader).get_deployment(
            DeploymentInput(namespace="team-a", deployment_name="api")
        )
    )

    assert output.selector == {"app": "api"}
    assert output.ready_replicas == 0
    container = output.containers[0]
    assert container.liveness_probe is not None
    assert container.liveness_probe.initial_delay_seconds == 10
    assert container.liveness_probe.failure_threshold == 3
    assert container.startup_probe is None
    assert container.resources.requests["cpu"] == "100m"

    environment = {item.name: item for item in container.environment}
    assert environment["STARTUP_DELAY_SECONDS"].value == "40"
    assert environment["DATABASE_PASSWORD"].redacted is True
    assert environment["DATABASE_PASSWORD"].value is None
    assert environment["API_TOKEN"].kind == "secret_key_ref"
    assert environment["API_TOKEN"].redacted is True
    assert environment["POD_NAME"].kind == "field_ref"
    assert environment["LOG_LEVEL"].kind == "config_map_key_ref"
    assert "do-not-leak" not in output.model_dump_json()
