"""Bounded topology and resource observations from KubernetesReaderV2."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal, cast

from pydantic import JsonValue, ValidationError

from opspilot.integrations.kubernetes.errors import (
    KubernetesDataError, KubernetesNotFoundError,
)
from opspilot.integrations.kubernetes.models import JsonObject
from opspilot.integrations.kubernetes.targeting import _valid_label
from opspilot.integrations.kubernetes.targeting_v2 import MultiPodResolverV2
from opspilot.integrations.kubernetes.v2_client import KubernetesReaderV2
from opspilot.tools.kubernetes.v2_models import (
    ContainerUsageV2, EndpointPortV2, EndpointSliceV2, EndpointV2,
    EndpointsInputV2, EndpointsOutputV2, IngressInputV2, IngressOutputV2,
    IngressRouteV2, PodUsageV2, ResourceUsageInputV2, ResourceUsageOutputV2,
    ServiceInputV2, ServiceOutputV2, ServicePortV2,
)

_PATH = re.compile(r"^/[A-Za-z0-9._~/%-]*$")
_WINDOW = re.compile(r"^[0-9]+(?:\.[0-9]+)?s$")


class KubernetesToolHandlersV2:
    def __init__(
        self, reader: KubernetesReaderV2,
        *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._resolver = MultiPodResolverV2(reader)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get_service(self, arguments: ServiceInputV2) -> ServiceOutputV2:
        raw = await self._reader.read_service(
            namespace=arguments.namespace, service_name=arguments.service_name
        )
        name, uid, version = _identity(raw, arguments.namespace, arguments.service_name)
        spec = _object(raw.get("spec"), "Service spec")
        selector_raw = spec.get("selector")
        if selector_raw is None:
            selector_raw = {}
        selector = _object(selector_raw, "Service selector")
        if len(selector) > 20 or any(
            not isinstance(value, str) or not _valid_label(key, value)
            for key, value in selector.items()
        ):
            raise KubernetesDataError("Kubernetes Service selector is invalid")
        ports = _list(spec.get("ports"), "Service ports", 32)
        try:
            return ServiceOutputV2(
                namespace=arguments.namespace, name=name, uid=uid,
                resource_version=version,
                selector={key: cast(str, selector[key]) for key in sorted(selector)},
                ports=tuple(
                    ServicePortV2(
                        name=_optional_text(item.get("name"), "Service port name", 63),
                        protocol=_protocol(item.get("protocol", "TCP")),
                        port=_int(item.get("port"), "Service port"),
                        target_port=_port(item.get("targetPort", item.get("port"))),
                    )
                    for item in ports
                ),
                service_type=_service_type(spec.get("type", "ClusterIP")),
            )
        except ValidationError:
            raise KubernetesDataError("Kubernetes Service data is invalid") from None

    async def get_endpoints(self, arguments: EndpointsInputV2) -> EndpointsOutputV2:
        raw_slices, truncated = await self._reader.list_endpoint_slices(
            namespace=arguments.namespace, service_name=arguments.service_name,
            limit=20,
        )
        if len(raw_slices) > 20:
            truncated = True
        ordered = sorted(raw_slices[:20], key=lambda item: _sort_name(item))
        normalized: list[EndpointSliceV2] = []
        remaining = 100
        for raw in ordered:
            name, uid, version = _identity(raw, arguments.namespace, None)
            metadata = _object(raw.get("metadata"), "EndpointSlice metadata")
            labels = _object(metadata.get("labels"), "EndpointSlice labels")
            if labels.get("kubernetes.io/service-name") != arguments.service_name:
                raise KubernetesDataError("EndpointSlice belongs to another Service")
            raw_ports = _list(raw.get("ports"), "EndpointSlice ports", 32)
            raw_endpoints = _list(raw.get("endpoints"), "EndpointSlice endpoints", 1000)
            if len(raw_endpoints) > remaining:
                truncated = True
            try:
                endpoints = tuple(sorted(
                    (_endpoint(item) for item in raw_endpoints[:remaining]),
                    key=lambda item: (item.target_name or "", item.target_uid or ""),
                ))
                normalized.append(EndpointSliceV2(
                    name=name, uid=uid, resource_version=version,
                    address_type=_address_type(raw.get("addressType")),
                    ports=tuple(_endpoint_port(item) for item in raw_ports),
                    endpoints=endpoints,
                ))
            except ValidationError:
                raise KubernetesDataError("Kubernetes EndpointSlice data is invalid") from None
            remaining -= len(endpoints)
        return EndpointsOutputV2(
            namespace=arguments.namespace, service_name=arguments.service_name,
            slices=tuple(normalized), truncated=truncated,
            observed_at=_aware(self._clock(), "EndpointSlice observation time"),
        )

    async def get_ingress(self, arguments: IngressInputV2) -> IngressOutputV2:
        raw = await self._reader.read_ingress(
            namespace=arguments.namespace, ingress_name=arguments.ingress_name
        )
        name, uid, version = _identity(raw, arguments.namespace, arguments.ingress_name)
        spec = _object(raw.get("spec"), "Ingress spec")
        rules = _list(spec.get("rules"), "Ingress rules", 100)
        routes: list[IngressRouteV2] = []
        default = spec.get("defaultBackend")
        if default is not None:
            service_name, service_port = _backend(default)
            routes.append(IngressRouteV2(
                host_sha256=None, path="/", path_type="ImplementationSpecific",
                service_name=service_name, service_port=service_port,
            ))
        for rule in rules:
            host = _optional_text(rule.get("host"), "Ingress host", 253)
            host_hash = sha256(host.lower().encode("utf-8")).hexdigest() if host else None
            http = _object(rule.get("http"), "Ingress HTTP rule")
            paths = _list(http.get("paths"), "Ingress paths", 100)
            for path in paths:
                if len(routes) >= 100:
                    raise KubernetesDataError("Kubernetes Ingress has too many routes")
                route_path = _text(path.get("path", "/"), "Ingress path", 256)
                if _PATH.fullmatch(route_path) is None:
                    raise KubernetesDataError("Kubernetes Ingress path is invalid")
                service_name, service_port = _backend(path.get("backend"))
                try:
                    routes.append(IngressRouteV2(
                        host_sha256=host_hash, path=route_path,
                        path_type=_path_type(path.get("pathType")),
                        service_name=service_name, service_port=service_port,
                    ))
                except ValidationError:
                    raise KubernetesDataError("Kubernetes Ingress route is invalid") from None
        routes.sort(key=lambda item: (
            item.host_sha256 or "", item.path, item.service_name, str(item.service_port)
        ))
        return IngressOutputV2(
            namespace=arguments.namespace, name=name, uid=uid,
            resource_version=version, routes=tuple(routes),
        )

    async def get_resource_usage(
        self, arguments: ResourceUsageInputV2,
    ) -> ResourceUsageOutputV2:
        snapshot, selector = await self._resolver.snapshot(
            namespace=arguments.namespace, deployment_name=arguments.deployment_name,
            limit=arguments.max_pods,
        )
        active = {pod.name: pod for pod in snapshot.pods if not pod.terminating}
        if not active:
            raise KubernetesNotFoundError("No active Pod matched the Deployment")
        metrics, truncated = await self._reader.list_pod_metrics(
            namespace=arguments.namespace, label_selector=selector,
            limit=arguments.max_pods,
        )
        if truncated or len(metrics) > arguments.max_pods:
            raise KubernetesDataError("Kubernetes Metrics snapshot is incomplete")
        observed = _aware(self._clock(), "Metrics observation time")
        usages: list[PodUsageV2] = []
        names: set[str] = set()
        sample_count = 0
        for raw in metrics:
            metadata = _object(raw.get("metadata"), "PodMetrics metadata")
            if metadata.get("namespace") != arguments.namespace:
                raise KubernetesDataError("PodMetrics namespace differs from request")
            name = _text(metadata.get("name"), "PodMetrics name", 253)
            pod = active.get(name)
            if pod is None or name in names:
                raise KubernetesDataError("PodMetrics target is not unique in Deployment")
            metric_uid = metadata.get("uid")
            if metric_uid not in (None, "", pod.uid):
                raise KubernetesDataError("PodMetrics UID differs from Pod snapshot")
            sampled = _timestamp(raw.get("timestamp"), "PodMetrics timestamp")
            if sampled > observed + timedelta(seconds=30) or observed - sampled > timedelta(minutes=5):
                raise KubernetesDataError("PodMetrics sample is stale or in the future")
            window = _text(raw.get("window"), "PodMetrics window", 64)
            if _WINDOW.fullmatch(window) is None:
                raise KubernetesDataError("PodMetrics window is invalid")
            containers = _list(raw.get("containers"), "PodMetrics containers", 100)
            if not containers:
                raise KubernetesDataError("PodMetrics contains no container samples")
            sample_count += len(containers)
            if sample_count > 100:
                raise KubernetesDataError("PodMetrics exceeds V2 Evidence limit")
            try:
                usages.append(PodUsageV2(
                    pod_name=name, pod_uid=pod.uid, sampled_at=sampled,
                    window=window,
                    containers=tuple(
                        ContainerUsageV2(
                            name=_text(item.get("name"), "PodMetrics container", 63),
                            cpu=_text(_object(item.get("usage"), "container usage").get("cpu"), "CPU quantity", 64),
                            memory=_text(_object(item.get("usage"), "container usage").get("memory"), "memory quantity", 64),
                        )
                        for item in containers
                    ),
                ))
            except ValidationError:
                raise KubernetesDataError("PodMetrics quantity is invalid") from None
            names.add(name)
        if not usages:
            raise KubernetesNotFoundError("No fresh Metrics samples matched the Deployment")
        usages.sort(key=lambda item: item.pod_name)
        try:
            return ResourceUsageOutputV2(
                namespace=arguments.namespace, deployment_name=arguments.deployment_name,
                snapshot=snapshot, usages=tuple(usages),
                missing_pods=tuple(sorted({pod.name for pod in snapshot.pods} - names)),
                observed_at=observed,
            )
        except ValidationError:
            raise KubernetesDataError("Kubernetes resource usage data is invalid") from None


def _sort_name(raw: JsonObject) -> str:
    metadata = _object(raw.get("metadata"), "resource metadata")
    return _text(metadata.get("name"), "resource name", 253)


def _identity(
    raw: JsonObject, namespace: str, expected_name: str | None,
) -> tuple[str, str, str]:
    metadata = _object(raw.get("metadata"), "resource metadata")
    if metadata.get("namespace") != namespace:
        raise KubernetesDataError("Kubernetes resource namespace differs from request")
    name = _text(metadata.get("name"), "resource name", 253)
    if expected_name is not None and name != expected_name:
        raise KubernetesDataError("Kubernetes resource name differs from request")
    return (
        name, _text(metadata.get("uid"), "resource UID", 128),
        _text(metadata.get("resourceVersion"), "resourceVersion", 128),
    )


def _endpoint_port(raw: JsonObject) -> EndpointPortV2:
    return EndpointPortV2(
        name=_optional_text(raw.get("name"), "Endpoint port name", 63),
        protocol=_protocol(raw.get("protocol", "TCP")),
        port=_optional_int(raw.get("port"), "Endpoint port"),
    )


def _endpoint(raw: JsonObject) -> EndpointV2:
    addresses = raw.get("addresses")
    if (
        not isinstance(addresses, list) or not 1 <= len(addresses) <= 16
        or any(not isinstance(value, str) or not value or len(value) > 253 for value in addresses)
    ):
        raise KubernetesDataError("Endpoint addresses are invalid")
    conditions = _object(raw.get("conditions", {}), "Endpoint conditions")
    reference = raw.get("targetRef")
    target = _object(reference, "Endpoint targetRef") if reference is not None else None
    is_pod = target is not None and target.get("kind") == "Pod"
    return EndpointV2(
        address_count=len(addresses),
        ready=_optional_bool(conditions.get("ready"), "Endpoint ready"),
        serving=_optional_bool(conditions.get("serving"), "Endpoint serving"),
        terminating=_optional_bool(conditions.get("terminating"), "Endpoint terminating"),
        target_kind="Pod" if is_pod else None,
        target_name=_text(target.get("name"), "Endpoint Pod name", 253) if is_pod and target else None,
        target_uid=_text(target.get("uid"), "Endpoint Pod UID", 128) if is_pod and target else None,
    )


def _backend(raw: JsonValue | None) -> tuple[str, int | str]:
    backend = _object(raw, "Ingress backend")
    service = _object(backend.get("service"), "Ingress backend Service")
    name = _text(service.get("name"), "Ingress Service name", 253)
    port = _object(service.get("port"), "Ingress Service port")
    number = port.get("number")
    label = port.get("name")
    if (number is None) == (label is None):
        raise KubernetesDataError("Ingress Service port is ambiguous")
    selected = _port(number if number is not None else label)
    if selected is None:
        raise KubernetesDataError("Ingress Service port is missing")
    return name, selected


def _port(value: JsonValue | None) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise KubernetesDataError("Kubernetes port is invalid")
    if isinstance(value, int) and 1 <= value <= 65_535:
        return value
    if isinstance(value, str) and re.fullmatch(r"^[a-z][a-z0-9-]{0,62}$", value):
        return value
    raise KubernetesDataError("Kubernetes port is invalid")


def _protocol(value: JsonValue | None) -> Literal["TCP", "UDP", "SCTP"]:
    if value == "TCP":
        return "TCP"
    if value == "UDP":
        return "UDP"
    if value == "SCTP":
        return "SCTP"
    raise KubernetesDataError("Kubernetes port protocol is invalid")


def _service_type(value: JsonValue | None) -> Literal["ClusterIP", "NodePort", "LoadBalancer", "ExternalName"]:
    if value == "ClusterIP":
        return "ClusterIP"
    if value == "NodePort":
        return "NodePort"
    if value == "LoadBalancer":
        return "LoadBalancer"
    if value == "ExternalName":
        return "ExternalName"
    raise KubernetesDataError("Kubernetes Service type is invalid")


def _address_type(value: JsonValue | None) -> Literal["IPv4", "IPv6", "FQDN"]:
    if value == "IPv4":
        return "IPv4"
    if value == "IPv6":
        return "IPv6"
    if value == "FQDN":
        return "FQDN"
    raise KubernetesDataError("Kubernetes EndpointSlice address type is invalid")


def _path_type(value: JsonValue | None) -> Literal["Exact", "Prefix", "ImplementationSpecific"]:
    if value == "Exact":
        return "Exact"
    if value == "Prefix":
        return "Prefix"
    if value == "ImplementationSpecific":
        return "ImplementationSpecific"
    raise KubernetesDataError("Kubernetes Ingress path type is invalid")


def _object(value: JsonValue | None, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _list(value: JsonValue | None, field: str, limit: int) -> tuple[JsonObject, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, dict) for item in value):
        raise KubernetesDataError(f"Kubernetes {field} is invalid or exceeds limit")
    return tuple(cast(JsonObject, item) for item in value)


def _text(value: JsonValue | None, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _optional_text(value: JsonValue | None, field: str, limit: int) -> str | None:
    return None if value in (None, "") else _text(value, field, limit)


def _int(value: JsonValue | None, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _optional_int(value: JsonValue | None, field: str) -> int | None:
    return None if value is None else _int(value, field)


def _optional_bool(value: JsonValue | None, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _timestamp(value: JsonValue | None, field: str) -> datetime:
    text = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise KubernetesDataError(f"Kubernetes {field} is invalid") from None
    return _aware(parsed, field)


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise KubernetesDataError(f"Kubernetes {field} is missing timezone")
    return value.astimezone(UTC)
