"""Deterministic exact-pod and single-replica Deployment target resolution."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import JsonValue, ValidationError

from opspilot.integrations.kubernetes.client import KubernetesReader
from opspilot.integrations.kubernetes.errors import (
    KubernetesAmbiguousTargetError,
    KubernetesDataError,
    KubernetesNotFoundError,
)
from opspilot.integrations.kubernetes.models import (
    JsonObject,
    PodTarget,
    ResolvedPod,
)

_DNS_PREFIX_PATTERN = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$"
)
_LABEL_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$"
)


class PodTargetResolver:
    """Resolve a validated target without making arbitrary pod choices."""

    def __init__(self, reader: KubernetesReader) -> None:
        self._reader = reader

    async def resolve(self, target: PodTarget) -> ResolvedPod:
        if target.pod_name is not None:
            pod = await self._reader.read_pod(
                namespace=target.namespace,
                pod_name=target.pod_name,
            )
            return _resolved_pod(
                pod,
                expected_namespace=target.namespace,
                expected_name=target.pod_name,
                requested_container=target.container_name,
                source="pod",
                workload_name=None,
            )

        workload_name = target.workload_name
        if workload_name is None:
            raise KubernetesDataError("Pod target is missing a resource")

        deployment = await self._reader.read_deployment(
            namespace=target.namespace,
            deployment_name=workload_name,
        )
        selector = _deployment_selector(
            deployment,
            expected_namespace=target.namespace,
            expected_name=workload_name,
        )
        pods = await self._reader.list_pods(
            namespace=target.namespace,
            label_selector=selector,
        )
        active_pods = tuple(pod for pod in pods if not _is_terminating(pod))
        if not active_pods:
            raise KubernetesNotFoundError(
                "No active pod matched the Kubernetes workload"
            )
        if len(active_pods) > 1:
            raise KubernetesAmbiguousTargetError(
                "Multiple active pods matched the Kubernetes workload"
            )

        return _resolved_pod(
            active_pods[0],
            expected_namespace=target.namespace,
            expected_name=None,
            requested_container=target.container_name,
            source="deployment",
            workload_name=workload_name,
        )


def _deployment_selector(
    deployment: JsonObject,
    *,
    expected_namespace: str,
    expected_name: str,
) -> str:
    metadata = _required_object(deployment.get("metadata"), "deployment metadata")
    _require_resource_name(
        metadata,
        expected_namespace=expected_namespace,
        expected_name=expected_name,
        resource="deployment",
    )
    spec = _required_object(deployment.get("spec"), "deployment spec")
    selector = _required_object(spec.get("selector"), "deployment selector")
    match_expressions = selector.get("matchExpressions")
    if match_expressions not in (None, []):
        raise KubernetesDataError(
            "Kubernetes deployment selector match expressions are unsupported"
        )
    match_labels = _required_object(
        selector.get("matchLabels"),
        "deployment matchLabels",
    )
    if not match_labels:
        raise KubernetesDataError(
            "Kubernetes deployment selector has no match labels"
        )

    labels: list[tuple[str, str]] = []
    for key, value in match_labels.items():
        if not isinstance(value, str) or not _valid_label(key, value):
            raise KubernetesDataError(
                "Kubernetes deployment selector is invalid"
            )
        labels.append((key, value))
    labels.sort()
    return ",".join(f"{key}={value}" for key, value in labels)


def _valid_label(key: str, value: str) -> bool:
    parts = key.split("/")
    if len(parts) > 2:
        return False
    name = parts[-1]
    if _LABEL_NAME_PATTERN.fullmatch(name) is None:
        return False
    if len(parts) == 2:
        prefix = parts[0]
        if len(prefix) > 253 or _DNS_PREFIX_PATTERN.fullmatch(prefix) is None:
            return False
    return value == "" or _LABEL_NAME_PATTERN.fullmatch(value) is not None


def _is_terminating(pod: JsonObject) -> bool:
    metadata = _required_object(pod.get("metadata"), "pod metadata")
    return metadata.get("deletionTimestamp") is not None


def _resolved_pod(
    pod: JsonObject,
    *,
    expected_namespace: str,
    expected_name: str | None,
    requested_container: str | None,
    source: Literal["pod", "deployment"],
    workload_name: str | None,
) -> ResolvedPod:
    metadata = _required_object(pod.get("metadata"), "pod metadata")
    pod_name, pod_uid = _require_identity(
        metadata,
        expected_namespace=expected_namespace,
        expected_name=expected_name,
        resource="pod",
    )
    spec = _required_object(pod.get("spec"), "pod spec")
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        raise KubernetesDataError("Kubernetes pod has no containers")

    container_names: list[str] = []
    for item in containers:
        container = _required_object(item, "pod container")
        name = container.get("name")
        if not isinstance(name, str) or not name:
            raise KubernetesDataError(
                "Kubernetes pod container name is invalid"
            )
        container_names.append(name)
    if len(set(container_names)) != len(container_names):
        raise KubernetesDataError(
            "Kubernetes pod container names are not unique"
        )
    if (
        requested_container is not None
        and requested_container not in container_names
    ):
        raise KubernetesNotFoundError(
            "Requested container was not found in the Kubernetes pod"
        )

    try:
        return ResolvedPod(
            namespace=expected_namespace,
            pod_name=pod_name,
            pod_uid=pod_uid,
            container_names=tuple(container_names),
            requested_container=requested_container,
            source=source,
            workload_name=workload_name,
        )
    except ValidationError:
        raise KubernetesDataError(
            "Kubernetes pod identity is invalid"
        ) from None


def _require_identity(
    metadata: JsonObject,
    *,
    expected_namespace: str,
    expected_name: str | None,
    resource: str,
) -> tuple[str, str]:
    name = _require_resource_name(
        metadata,
        expected_namespace=expected_namespace,
        expected_name=expected_name,
        resource=resource,
    )
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise KubernetesDataError(
            f"Kubernetes {resource} identity is incomplete"
        )
    return name, uid


def _require_resource_name(
    metadata: JsonObject,
    *,
    expected_namespace: str,
    expected_name: str | None,
    resource: str,
) -> str:
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if not isinstance(namespace, str) or not isinstance(name, str):
        raise KubernetesDataError(
            f"Kubernetes {resource} identity is incomplete"
        )
    if namespace != expected_namespace:
        raise KubernetesDataError(
            f"Kubernetes {resource} namespace did not match the request"
        )
    if expected_name is not None and name != expected_name:
        raise KubernetesDataError(
            f"Kubernetes {resource} name did not match the request"
        )
    return name


def _required_object(
    value: JsonValue | None,
    field: str,
) -> JsonObject:
    if not isinstance(value, dict):
        raise KubernetesDataError(
            f"Kubernetes response is missing {field}"
        )
    return value
