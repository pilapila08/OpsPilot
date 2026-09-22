import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes import (
    JsonObject,
    KubernetesIntegrationError,
    KubernetesNotFoundError,
    KubernetesPermissionError,
    KubernetesTimeoutError,
    KubernetesTransientError,
)
from opspilot.tools import (
    ToolInvocation,
    ToolResponse,
    ToolRiskLevel,
)
from opspilot.tools.kubernetes import build_kubernetes_registry

ROOT = Path(__file__).resolve().parents[3]
CASE_ROOT = ROOT / "fixtures" / "cases" / "crashloop-liveness-v1"


def _read_json(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    return cast(JsonObject, value)


class CaseReader:
    def __init__(self) -> None:
        pod_response = _read_json(
            CASE_ROOT / "responses" / "fault" / "pod-status.json"
        )
        self.pod = cast(JsonObject, pod_response["data"])
        metadata = cast(JsonObject, self.pod["metadata"])
        metadata["uid"] = "pod-uid"
        self.pod["spec"] = {"containers": [{"name": "slow-start-api"}]}

        events_response = _read_json(
            CASE_ROOT / "responses" / "fault" / "pod-events.json"
        )
        event_data = cast(JsonObject, events_response["data"])
        event_items = cast(list[JsonValue], event_data["items"])
        normalized_events: list[JsonObject] = []
        for index, item in enumerate(event_items):
            event = cast(JsonObject, item)
            event["metadata"] = {"name": f"fixture-event-{index}"}
            involved = cast(JsonObject, event["involvedObject"])
            involved["uid"] = "pod-uid"
            normalized_events.append(event)
        self.events = tuple(normalized_events)

        previous_response = _read_json(
            CASE_ROOT / "responses" / "fault" / "previous-logs.json"
        )
        previous_data = cast(JsonObject, previous_response["data"])
        self.log_content = cast(str, previous_data["content"])
        self.deployment = _read_json(
            CASE_ROOT / "manifests" / "broken-deployment.json"
        )
        deployment_metadata = cast(JsonObject, self.deployment["metadata"])
        deployment_metadata["generation"] = 1
        self.deployment["status"] = {
            "replicas": 1,
            "readyReplicas": 0,
            "unavailableReplicas": 1,
        }

    async def read_pod(self, *, namespace: str, pod_name: str) -> JsonObject:
        del namespace, pod_name
        return self.pod

    async def list_pods(
        self,
        *,
        namespace: str,
        label_selector: str,
    ) -> tuple[JsonObject, ...]:
        del namespace, label_selector
        return (self.pod,)

    async def list_events(
        self,
        *,
        namespace: str,
        field_selector: str,
        limit: int,
    ) -> tuple[JsonObject, ...]:
        del namespace, field_selector, limit
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
        del (
            namespace,
            pod_name,
            container_name,
            previous,
            tail_lines,
            since_seconds,
            max_bytes,
        )
        return self.log_content

    async def read_deployment(
        self,
        *,
        namespace: str,
        deployment_name: str,
    ) -> JsonObject:
        del namespace, deployment_name
        return self.deployment


def _invoke(
    reader: CaseReader,
    *,
    call_id: str,
    tool: str,
    arguments: dict[str, JsonValue],
) -> ToolResponse:
    registry = build_kubernetes_registry(reader)
    return asyncio.run(
        registry.invoke(
            ToolInvocation(
                call_id=call_id,
                tool=tool,
                arguments=arguments,
            )
        )
    )


def test_registry_exposes_exactly_five_read_only_descriptors() -> None:
    descriptors = build_kubernetes_registry(CaseReader()).descriptors()

    assert [descriptor.name for descriptor in descriptors] == [
        "k8s.get_deployment",
        "k8s.get_pod_events",
        "k8s.get_pod_logs",
        "k8s.get_pod_status",
        "k8s.get_previous_logs",
    ]
    assert all(
        descriptor.risk_level is ToolRiskLevel.READ_ONLY
        for descriptor in descriptors
    )
    assert all(descriptor.source == "kubernetes" for descriptor in descriptors)
    assert all(descriptor.version == "v1" for descriptor in descriptors)
    assert all(descriptor.timeout_seconds == 10 for descriptor in descriptors)
    assert all(descriptor.retry_policy.max_retries == 1 for descriptor in descriptors)
    assert all(
        "handler" not in descriptor.model_dump(mode="json")
        for descriptor in descriptors
    )


def test_all_five_tools_execute_through_registry_and_cover_case_facts() -> None:
    reader = CaseReader()
    pod_arguments: dict[str, JsonValue] = {
        "namespace": "opspilot-fixtures",
        "pod_name": "slow-start-api-7d9f4b6c8d-abcde",
    }
    status = _invoke(
        reader,
        call_id="call_status",
        tool="k8s.get_pod_status",
        arguments=pod_arguments,
    )
    events = _invoke(
        reader,
        call_id="call_events",
        tool="k8s.get_pod_events",
        arguments=pod_arguments,
    )
    current_logs = _invoke(
        reader,
        call_id="call_logs",
        tool="k8s.get_pod_logs",
        arguments={
            **pod_arguments,
            "container_name": "slow-start-api",
        },
    )
    previous_logs = _invoke(
        reader,
        call_id="call_previous",
        tool="k8s.get_previous_logs",
        arguments={
            **pod_arguments,
            "container_name": "slow-start-api",
        },
    )
    deployment = _invoke(
        reader,
        call_id="call_deployment",
        tool="k8s.get_deployment",
        arguments={
            "namespace": "opspilot-fixtures",
            "deployment_name": "slow-start-api",
        },
    )

    assert all(
        response.success
        for response in (
            status,
            events,
            current_logs,
            previous_logs,
            deployment,
        )
    )
    assert status.data is not None
    containers = cast(list[JsonValue], status.data["containers"])
    first_container = cast(JsonObject, containers[0])
    assert first_container["restart_count"] == 5
    assert first_container["reason"] == "CrashLoopBackOff"

    assert events.data is not None
    normalized_events = cast(list[JsonValue], events.data["events"])
    assert any(
        cast(JsonObject, item)["reason"] == "Unhealthy"
        for item in normalized_events
    )

    assert previous_logs.data is not None
    assert previous_logs.data["previous"] is True
    assert "configured_startup_delay_seconds=40" in cast(
        str,
        previous_logs.data["content"],
    )

    assert deployment.data is not None
    deployment_containers = cast(
        list[JsonValue],
        deployment.data["containers"],
    )
    deployment_container = cast(JsonObject, deployment_containers[0])
    liveness = cast(JsonObject, deployment_container["liveness_probe"])
    assert liveness["initial_delay_seconds"] == 10
    assert liveness["failure_threshold"] == 3
    assert deployment_container["startup_probe"] is None


class FailingReader(CaseReader):
    def __init__(self, failure: KubernetesIntegrationError) -> None:
        super().__init__()
        self.failure = failure

    async def read_pod(self, *, namespace: str, pod_name: str) -> JsonObject:
        del namespace, pod_name
        raise self.failure


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (
            KubernetesNotFoundError("resource was not found"),
            ErrorCode.INVALID_ARGUMENT,
            False,
        ),
        (
            KubernetesPermissionError("access was denied"),
            ErrorCode.PERMISSION_DENIED,
            False,
        ),
        (
            KubernetesTimeoutError("request timed out"),
            ErrorCode.TOOL_TIMEOUT,
            True,
        ),
        (
            KubernetesTransientError("service unavailable"),
            ErrorCode.EXTERNAL_SERVICE_ERROR,
            True,
        ),
    ],
)
def test_registry_preserves_sanitized_kubernetes_error_mapping(
    failure: KubernetesIntegrationError,
    code: ErrorCode,
    retryable: bool,
) -> None:
    response = _invoke(
        FailingReader(failure),
        call_id="call_failure",
        tool="k8s.get_pod_status",
        arguments={
            "namespace": "opspilot-fixtures",
            "pod_name": "slow-start-api-7d9f4b6c8d-abcde",
        },
    )

    assert response.success is False
    assert response.error is not None
    assert response.error.code is code
    assert response.error.retryable is retryable
