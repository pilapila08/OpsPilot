import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import JsonValue

from opspilot.evidence import EvidenceExtractorRegistry
from opspilot.integrations.kubernetes import JsonObject
from opspilot.integrations.kubernetes.errors import KubernetesDataError, KubernetesNotFoundError
from opspilot.integrations.kubernetes.v2_client import KubernetesReaderV2
from opspilot.tools import ToolInvocation, ToolRiskLevel
from opspilot.tools.kubernetes.v2_handlers import KubernetesToolHandlersV2
from opspilot.tools.kubernetes.v2_models import (
    EndpointsInputV2, EndpointsOutputV2, IngressInputV2,
    ResourceUsageInputV2, ResourceUsageOutputV2, ServiceInputV2,
)
from opspilot.tools.kubernetes.v2_registry import build_kubernetes_registry_v2


class FakeV2Reader:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.service: JsonObject = {
            "metadata": {
                "namespace": "team-a", "name": "api", "uid": "svc-uid",
                "resourceVersion": "12", "annotations": {"token": "do-not-leak"},
            },
            "spec": {
                "type": "ClusterIP", "selector": {"app": "api"},
                "ports": [{"name": "http", "protocol": "TCP", "port": 80, "targetPort": 8080}],
            },
        }
        self.slices: tuple[JsonObject, ...] = ({
            "metadata": {
                "namespace": "team-a", "name": "api-slice", "uid": "slice-uid",
                "resourceVersion": "13",
                "labels": {"kubernetes.io/service-name": "api"},
            },
            "addressType": "IPv4",
            "ports": [{"name": "http", "protocol": "TCP", "port": 9999}],
            "endpoints": [
                {"addresses": ["10.0.0.1"], "conditions": {"ready": True, "serving": True, "terminating": False},
                 "targetRef": {"kind": "Pod", "name": "api-001", "uid": "pod-uid-1"}},
                {"addresses": ["10.0.0.2"], "conditions": {"ready": False, "serving": False, "terminating": False},
                 "targetRef": {"kind": "Pod", "name": "api-002", "uid": "pod-uid-2"}},
            ],
        },)
        self.ingress: JsonObject = {
            "metadata": {
                "namespace": "team-a", "name": "edge", "uid": "ing-uid",
                "resourceVersion": "14", "annotations": {"secret": "do-not-leak"},
            },
            "spec": {"rules": [{
                "host": "api.internal.example",
                "http": {"paths": [{
                    "path": "/api", "pathType": "Prefix",
                    "backend": {"service": {"name": "api", "port": {"number": 80}}},
                }]},
            }]},
        }
        self.deployment: JsonObject = {
            "metadata": {
                "namespace": "team-a", "name": "api", "uid": "dep-uid",
                "resourceVersion": "15", "generation": 2,
            },
            "spec": {"selector": {"matchLabels": {"app": "api"}}},
        }
        self.pods: tuple[JsonObject, ...] = (
            self._pod("api-002", "pod-uid-2", "hash-b", False),
            self._pod("api-001", "pod-uid-1", "hash-a", True),
        )
        self.metrics: tuple[JsonObject, ...] = ({
            "metadata": {"namespace": "team-a", "name": "api-001", "uid": "pod-uid-1"},
            "timestamp": now.isoformat(), "window": "15s",
            "containers": [{"name": "api", "usage": {"cpu": "200m", "memory": "120Mi"}}],
        },)
        self.metrics_truncated = False
        self.pods_truncated = False
        self.selector_calls: list[str] = []

    @staticmethod
    def _pod(name: str, uid: str, hash_value: str, ready: bool) -> JsonObject:
        return {
            "metadata": {
                "namespace": "team-a", "name": name, "uid": uid,
                "resourceVersion": "20", "labels": {"app": "api", "pod-template-hash": hash_value},
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            },
        }

    async def read_service(self, *, namespace: str, service_name: str) -> JsonObject:
        assert (namespace, service_name) == ("team-a", "api")
        return deepcopy(self.service)

    async def list_endpoint_slices(
        self, *, namespace: str, service_name: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        assert (namespace, service_name, limit) == ("team-a", "api", 20)
        return deepcopy(self.slices), False

    async def read_ingress(self, *, namespace: str, ingress_name: str) -> JsonObject:
        assert (namespace, ingress_name) == ("team-a", "edge")
        return deepcopy(self.ingress)

    async def read_deployment(self, *, namespace: str, deployment_name: str) -> JsonObject:
        assert (namespace, deployment_name) == ("team-a", "api")
        return deepcopy(self.deployment)

    async def list_pods_bounded(
        self, *, namespace: str, label_selector: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        assert namespace == "team-a" and limit == 20
        self.selector_calls.append(label_selector)
        return deepcopy(self.pods), self.pods_truncated

    async def list_pod_metrics(
        self, *, namespace: str, label_selector: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        assert namespace == "team-a" and limit == 20
        self.selector_calls.append(label_selector)
        return deepcopy(self.metrics), self.metrics_truncated


def _reader(fake: FakeV2Reader) -> KubernetesReaderV2:
    return cast(KubernetesReaderV2, fake)


def test_four_v2_tools_are_read_only_and_emit_bounded_evidence() -> None:
    fake = FakeV2Reader()
    registry = build_kubernetes_registry_v2(_reader(fake))
    descriptors = registry.descriptors()
    assert len(descriptors) == 9
    assert all(item.risk_level is ToolRiskLevel.READ_ONLY for item in descriptors)
    expected = {
        "k8s.get_service", "k8s.get_endpoints",
        "k8s.get_ingress", "k8s.get_resource_usage",
    }
    assert expected.issubset({item.name for item in descriptors})
    calls = (
        ToolInvocation(call_id="call_service", tool="k8s.get_service", arguments={"namespace": "team-a", "service_name": "api"}),
        ToolInvocation(call_id="call_endpoints", tool="k8s.get_endpoints", arguments={"namespace": "team-a", "service_name": "api"}),
        ToolInvocation(call_id="call_ingress", tool="k8s.get_ingress", arguments={"namespace": "team-a", "ingress_name": "edge"}),
        ToolInvocation(call_id="call_usage", tool="k8s.get_resource_usage", arguments={"namespace": "team-a", "deployment_name": "api"}),
    )
    extractor = EvidenceExtractorRegistry()
    sources: set[str] = set()
    for index, invocation in enumerate(calls, start=1):
        response = asyncio.run(registry.invoke(invocation))
        assert response.success, response.error
        assert response.data is not None

        def new_evidence_id() -> str:
            return f"ev_{index:03d}"

        evidence = extractor.extract(
            invocation=invocation, response=response,
            trace_id="trace_001", tool_attempt_id=f"tool_{index:03d}",
            collected_at=datetime.now(UTC),
            evidence_id_factory=new_evidence_id,
        )
        assert evidence
        assert all(item.tool_call_id == f"tool_{index:03d}" and item.trace_id == "trace_001" for item in evidence)
        sources.update(item.source for item in evidence)
        encoded = json.dumps(response.data)
        assert "do-not-leak" not in encoded
        assert "api.internal.example" not in encoded
        assert "10.0.0.1" not in encoded
        if invocation.tool == "k8s.get_endpoints":
            parsed = EndpointsOutputV2.model_validate_json(encoded, strict=True)
            assert parsed.slices[0].ports[0].port == 9999
            assert evidence[0].attributes[1].value == 1
        if invocation.tool == "k8s.get_resource_usage":
            parsed_usage = ResourceUsageOutputV2.model_validate_json(encoded, strict=True)
            assert parsed_usage.snapshot.rollout_ambiguous is True
            assert parsed_usage.missing_pods == ("api-002",)
            assert [pod.name for pod in parsed_usage.snapshot.pods] == ["api-001", "api-002"]
    assert fake.selector_calls == ["app=api", "app=api"]
    assert sources == {
        "kubernetes_service", "kubernetes_endpoints",
        "kubernetes_ingress", "kubernetes_metrics",
    }


def test_empty_endpoints_and_invalid_slice_identity_are_explicit() -> None:
    fake = FakeV2Reader()
    handlers = KubernetesToolHandlersV2(_reader(fake))
    fake.slices = ()
    output = asyncio.run(handlers.get_endpoints(EndpointsInputV2(namespace="team-a", service_name="api")))
    assert output.slices == () and output.truncated is False
    fake.slices = FakeV2Reader().slices
    cast(dict[str, JsonValue], fake.slices[0]["metadata"])["labels"] = {
        "kubernetes.io/service-name": "other"
    }
    with pytest.raises(KubernetesDataError):
        asyncio.run(handlers.get_endpoints(EndpointsInputV2(namespace="team-a", service_name="api")))


def test_metrics_missing_stale_and_truncated_are_not_zero_usage() -> None:
    fake = FakeV2Reader()
    handlers = KubernetesToolHandlersV2(_reader(fake))
    input_data = ResourceUsageInputV2(namespace="team-a", deployment_name="api")
    fake.metrics = ()
    with pytest.raises(KubernetesNotFoundError):
        asyncio.run(handlers.get_resource_usage(input_data))
    fake.metrics = FakeV2Reader().metrics
    fake.metrics[0]["timestamp"] = (
        datetime.now(UTC) - timedelta(minutes=6)
    ).isoformat()
    with pytest.raises(KubernetesDataError):
        asyncio.run(handlers.get_resource_usage(input_data))
    fake.metrics = FakeV2Reader().metrics
    fake.metrics_truncated = True
    with pytest.raises(KubernetesDataError):
        asyncio.run(handlers.get_resource_usage(input_data))


def test_metrics_container_samples_are_bounded_to_evidence_limit() -> None:
    fake = FakeV2Reader()
    first = fake.metrics[0]
    first["containers"] = [
        {"name": f"container-{index}", "usage": {"cpu": "1m", "memory": "1Mi"}}
        for index in range(100)
    ]
    second = deepcopy(first)
    cast(dict[str, JsonValue], second["metadata"])["name"] = "api-002"
    cast(dict[str, JsonValue], second["metadata"])["uid"] = "pod-uid-2"
    second["containers"] = [{"name": "api", "usage": {"cpu": "1m", "memory": "1Mi"}}]
    fake.metrics = (first, second)
    with pytest.raises(KubernetesDataError, match="Evidence limit"):
        asyncio.run(KubernetesToolHandlersV2(_reader(fake)).get_resource_usage(
            ResourceUsageInputV2(namespace="team-a", deployment_name="api")
        ))


def test_ingress_backend_and_service_selector_are_schema_bounded() -> None:
    fake = FakeV2Reader()
    handlers = KubernetesToolHandlersV2(_reader(fake))
    ingress = asyncio.run(handlers.get_ingress(IngressInputV2(namespace="team-a", ingress_name="edge")))
    assert ingress.routes[0].service_name == "api"
    assert ingress.routes[0].service_port == 80
    fake.ingress["spec"] = {"rules": [{
        "host": "api.internal.example", "http": {"paths": [{
            "path": "/api?token=secret", "pathType": "Prefix",
            "backend": {"service": {"name": "api", "port": {"number": 80}}},
        }]},
    }]}
    with pytest.raises(KubernetesDataError):
        asyncio.run(handlers.get_ingress(IngressInputV2(namespace="team-a", ingress_name="edge")))
    fake.service["spec"] = {"selector": {"app": "api;cat /etc/passwd"}, "ports": []}
    with pytest.raises(KubernetesDataError):
        asyncio.run(handlers.get_service(ServiceInputV2(namespace="team-a", service_name="api")))


def test_multi_pod_snapshot_rejects_selector_mismatch_and_marks_truncation() -> None:
    fake = FakeV2Reader()
    handlers = KubernetesToolHandlersV2(_reader(fake))
    request = ResourceUsageInputV2(namespace="team-a", deployment_name="api")
    cast(dict[str, JsonValue], fake.pods[0]["metadata"])["labels"] = {"app": "other"}
    with pytest.raises(KubernetesDataError, match="selector"):
        asyncio.run(handlers.get_resource_usage(request))
    fake.pods = (
        FakeV2Reader._pod("api-001", "pod-uid-1", "hash-a", True),
    )
    fake.pods_truncated = True
    output = asyncio.run(handlers.get_resource_usage(request))
    assert output.snapshot.truncated is True
    assert output.snapshot.rollout_ambiguous is True
