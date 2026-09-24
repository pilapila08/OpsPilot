import asyncio
from typing import Any

import pytest
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes.errors import (
    KubernetesDataError, KubernetesPermissionError, KubernetesTransientError,
)
from opspilot.integrations.kubernetes.v2_client import KubernetesSdkReaderV2


class FakeClient:
    def sanitize_for_serialization(self, value: object) -> object:
        return value


class FakeCore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def read_namespaced_service(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {"metadata": {"name": kwargs["name"]}}

    def list_namespaced_pod(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {"items": [{"name": "one"}, {"name": "two"}, {"name": "three"}]}


class FakeDiscovery:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.result: object = {
            "metadata": {"continue": "opaque-token"},
            "items": [{"name": "slice-one"}],
        }

    def list_namespaced_endpoint_slice(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.result


class FakeNetworking:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def read_namespaced_ingress(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {"metadata": {"name": kwargs["name"]}}


class FakeMetrics:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    def list_namespaced_custom_object(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"items": [{"metadata": {"name": "api-001"}}]}


def _reader() -> tuple[KubernetesSdkReaderV2, FakeCore, FakeDiscovery, FakeNetworking, FakeMetrics]:
    core = FakeCore()
    discovery = FakeDiscovery()
    networking = FakeNetworking()
    metrics = FakeMetrics()
    reader = KubernetesSdkReaderV2(
        core_api=core, apps_api=object(), discovery_api=discovery,
        networking_api=networking, metrics_api=metrics,
        api_client=FakeClient(), request_timeout_seconds=7.5,
    )
    return reader, core, discovery, networking, metrics


def test_v2_sdk_calls_only_scoped_read_apis_with_bounds() -> None:
    reader, core, discovery, networking, metrics = _reader()
    assert asyncio.run(reader.read_service(namespace="team-a", service_name="api"))["metadata"] == {"name": "api"}
    assert core.calls[0] == {
        "name": "api", "namespace": "team-a", "_request_timeout": 7.5,
    }
    slices, truncated = asyncio.run(reader.list_endpoint_slices(
        namespace="team-a", service_name="api", limit=2,
    ))
    assert len(slices) == 1 and truncated is True
    assert discovery.calls[0] == {
        "namespace": "team-a", "label_selector": "kubernetes.io/service-name=api",
        "limit": 3, "_request_timeout": 7.5,
    }
    assert asyncio.run(reader.read_ingress(namespace="team-a", ingress_name="edge"))["metadata"] == {"name": "edge"}
    assert networking.calls[0]["name"] == "edge"
    pods, pod_truncated = asyncio.run(reader.list_pods_bounded(
        namespace="team-a", label_selector="app=api", limit=2,
    ))
    assert len(pods) == 2 and pod_truncated is True
    assert core.calls[-1]["label_selector"] == "app=api"
    samples, metrics_truncated = asyncio.run(reader.list_pod_metrics(
        namespace="team-a", label_selector="app=api", limit=2,
    ))
    assert len(samples) == 1 and metrics_truncated is False
    assert metrics.calls[0] == {
        "group": "metrics.k8s.io", "version": "v1beta1",
        "namespace": "team-a", "plural": "pods",
        "label_selector": "app=api", "limit": 3,
        "_request_timeout": 7.5,
    }


def test_v2_sdk_rejects_invalid_limit_and_real_bytes_shape_before_output() -> None:
    reader, _, discovery, _, metrics = _reader()
    with pytest.raises(KubernetesDataError):
        asyncio.run(reader.list_endpoint_slices(namespace="team-a", service_name="api", limit=101))
    with pytest.raises(KubernetesDataError):
        asyncio.run(reader.list_pod_metrics(namespace="team-a", label_selector="app=api", limit=0))
    assert discovery.calls == [] and metrics.calls == []
    discovery.result = b"not-json-object"
    with pytest.raises(KubernetesDataError):
        asyncio.run(reader.list_endpoint_slices(namespace="team-a", service_name="api", limit=2))
    discovery.result = {"items": [b"not-a-dict"]}
    with pytest.raises(KubernetesDataError):
        asyncio.run(reader.list_endpoint_slices(namespace="team-a", service_name="api", limit=2))


@pytest.mark.parametrize(
    ("status", "expected_type", "expected_code"),
    [
        (403, KubernetesPermissionError, ErrorCode.PERMISSION_DENIED),
        (503, KubernetesTransientError, ErrorCode.EXTERNAL_SERVICE_ERROR),
    ],
)
def test_v2_metrics_sdk_errors_are_classified_without_response_body(
    status: int, expected_type: type[Exception], expected_code: ErrorCode,
) -> None:
    reader, _, _, _, metrics = _reader()
    error = ApiException(status=status, reason="sensitive upstream body")
    error.body = "token=do-not-leak"
    metrics.error = error
    with pytest.raises(expected_type) as caught:
        asyncio.run(reader.list_pod_metrics(
            namespace="team-a", label_selector="app=api", limit=20,
        ))
    assert getattr(caught.value, "code") is expected_code
    assert "do-not-leak" not in str(caught.value)
    assert "sensitive upstream body" not in str(caught.value)
