"""Narrow V2 Kubernetes reads for topology and Metrics API snapshots."""

from __future__ import annotations

from typing import Any, Protocol, cast, runtime_checkable

from kubernetes import client  # type: ignore[import-untyped]

from opspilot.integrations.kubernetes.client import (
    KubernetesReader, KubernetesSdkReader, build_kubernetes_configuration,
)
from opspilot.integrations.kubernetes.errors import KubernetesDataError
from opspilot.integrations.kubernetes.models import JsonObject, KubernetesClientSettings


@runtime_checkable
class KubernetesReaderV2(KubernetesReader, Protocol):
    async def read_service(self, *, namespace: str, service_name: str) -> JsonObject: ...

    async def list_endpoint_slices(
        self, *, namespace: str, service_name: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]: ...

    async def read_ingress(self, *, namespace: str, ingress_name: str) -> JsonObject: ...

    async def list_pods_bounded(
        self, *, namespace: str, label_selector: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]: ...

    async def list_pod_metrics(
        self, *, namespace: str, label_selector: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]: ...


class KubernetesSdkReaderV2(KubernetesSdkReader):
    def __init__(
        self, *, core_api: Any, apps_api: Any, discovery_api: Any,
        networking_api: Any, metrics_api: Any, api_client: Any,
        request_timeout_seconds: float,
    ) -> None:
        super().__init__(
            core_api=core_api, apps_api=apps_api, api_client=api_client,
            request_timeout_seconds=request_timeout_seconds,
        )
        self._discovery_api = discovery_api
        self._networking_api = networking_api
        self._metrics_api = metrics_api
        self._v2_timeout = request_timeout_seconds

    async def read_service(self, *, namespace: str, service_name: str) -> JsonObject:
        result = await self._call(
            self._core_api.read_namespaced_service,
            operation="read service", name=service_name, namespace=namespace,
            _request_timeout=self._v2_timeout,
        )
        return self._sanitize_object(result, "service")

    async def list_endpoint_slices(
        self, *, namespace: str, service_name: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        _require_limit(limit)
        result = await self._call(
            self._discovery_api.list_namespaced_endpoint_slice,
            operation="list endpoint slices", namespace=namespace,
            label_selector=f"kubernetes.io/service-name={service_name}",
            limit=limit + 1, _request_timeout=self._v2_timeout,
        )
        return self._bounded_items(result, "EndpointSlice list", limit)

    async def read_ingress(self, *, namespace: str, ingress_name: str) -> JsonObject:
        result = await self._call(
            self._networking_api.read_namespaced_ingress,
            operation="read ingress", name=ingress_name, namespace=namespace,
            _request_timeout=self._v2_timeout,
        )
        return self._sanitize_object(result, "ingress")

    async def list_pods_bounded(
        self, *, namespace: str, label_selector: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        _require_limit(limit)
        result = await self._call(
            self._core_api.list_namespaced_pod,
            operation="list bounded pods", namespace=namespace,
            label_selector=label_selector, limit=limit + 1,
            _request_timeout=self._v2_timeout,
        )
        return self._bounded_items(result, "pod list", limit)

    async def list_pod_metrics(
        self, *, namespace: str, label_selector: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        _require_limit(limit)
        result = await self._call(
            self._metrics_api.list_namespaced_custom_object,
            operation="list pod metrics", group="metrics.k8s.io",
            version="v1beta1", namespace=namespace, plural="pods",
            label_selector=label_selector, limit=limit + 1,
            _request_timeout=self._v2_timeout,
        )
        return self._bounded_items(result, "pod metrics list", limit)

    def _bounded_items(
        self, value: object, resource: str, limit: int,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        serialized = self._sanitize_object(value, resource)
        items = serialized.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise KubernetesDataError(f"Kubernetes {resource} has invalid items")
        metadata = serialized.get("metadata")
        continuation = metadata.get("continue") if isinstance(metadata, dict) else None
        return (
            tuple(cast(JsonObject, item) for item in items[:limit]),
            len(items) > limit or bool(continuation),
        )


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise KubernetesDataError("Kubernetes list limit is outside V2 policy")


def build_kubernetes_reader_v2(settings: KubernetesClientSettings) -> KubernetesSdkReaderV2:
    configuration = build_kubernetes_configuration(settings)
    api_client = client.ApiClient(configuration=configuration)
    return KubernetesSdkReaderV2(
        core_api=client.CoreV1Api(api_client),
        apps_api=client.AppsV1Api(api_client),
        discovery_api=client.DiscoveryV1Api(api_client),
        networking_api=client.NetworkingV1Api(api_client),
        metrics_api=client.CustomObjectsApi(api_client),
        api_client=api_client,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
