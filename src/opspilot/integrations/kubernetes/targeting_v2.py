"""Deterministic Deployment-to-Pod snapshot without selecting one replica."""

from __future__ import annotations

import re

from pydantic import JsonValue, ValidationError

from opspilot.integrations.kubernetes.errors import KubernetesDataError
from opspilot.integrations.kubernetes.models import JsonObject
from opspilot.integrations.kubernetes.targeting import _deployment_selector
from opspilot.integrations.kubernetes.v2_client import KubernetesReaderV2
from opspilot.integrations.kubernetes.v2_models import MultiPodSnapshotV2, PodSnapshotV2

_HASH = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class MultiPodResolverV2:
    def __init__(self, reader: KubernetesReaderV2) -> None:
        self._reader = reader

    async def snapshot(
        self, *, namespace: str, deployment_name: str, limit: int,
    ) -> tuple[MultiPodSnapshotV2, str]:
        if not 1 <= limit <= 50:
            raise KubernetesDataError("Deployment Pod limit is outside V2 policy")
        deployment = await self._reader.read_deployment(
            namespace=namespace, deployment_name=deployment_name
        )
        selector = _deployment_selector(
            deployment, expected_namespace=namespace, expected_name=deployment_name
        )
        spec = _object(deployment.get("spec"), "deployment spec")
        selection = _object(spec.get("selector"), "deployment selector")
        match_labels = _object(selection.get("matchLabels"), "deployment matchLabels")
        metadata = _object(deployment.get("metadata"), "deployment metadata")
        uid = _text(metadata.get("uid"), "deployment UID")
        version = _text(metadata.get("resourceVersion"), "deployment resourceVersion")
        generation = metadata.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise KubernetesDataError("Kubernetes deployment generation is invalid")
        pods, truncated = await self._reader.list_pods_bounded(
            namespace=namespace, label_selector=selector, limit=limit
        )
        snapshots = tuple(sorted(
            (_pod_snapshot(item, namespace, match_labels) for item in pods[:limit]),
            key=lambda item: (item.name, item.uid),
        ))
        hashes = {item.pod_template_hash for item in snapshots if item.pod_template_hash}
        bounded_truncated = truncated or len(pods) > limit
        ambiguous = (
            len(hashes) > 1 or bounded_truncated
            or (len(snapshots) > 1 and any(
                item.pod_template_hash is None for item in snapshots
            ))
        )
        try:
            result = MultiPodSnapshotV2(
                namespace=namespace, deployment_name=deployment_name,
                deployment_uid=uid, deployment_resource_version=version,
                deployment_generation=generation, pods=snapshots,
                truncated=bounded_truncated,
                rollout_ambiguous=ambiguous,
            )
        except ValidationError:
            raise KubernetesDataError("Kubernetes Deployment Pod snapshot is invalid") from None
        return result, selector


def _pod_snapshot(
    pod: JsonObject, namespace: str, match_labels: JsonObject,
) -> PodSnapshotV2:
    metadata = _object(pod.get("metadata"), "pod metadata")
    if metadata.get("namespace") != namespace:
        raise KubernetesDataError("Kubernetes Pod namespace differs from Deployment")
    labels = _object(metadata.get("labels"), "pod labels")
    if any(labels.get(key) != value for key, value in match_labels.items()):
        raise KubernetesDataError("Kubernetes Pod labels differ from Deployment selector")
    raw_hash = labels.get("pod-template-hash")
    pod_hash = (
        raw_hash
        if isinstance(raw_hash, str) and _HASH.fullmatch(raw_hash) is not None
        else None
    )
    status = _object(pod.get("status"), "pod status")
    conditions = status.get("conditions")
    if conditions is None:
        conditions = []
    if not isinstance(conditions, list) or len(conditions) > 100:
        raise KubernetesDataError("Kubernetes Pod conditions are invalid")
    ready = False
    for raw in conditions:
        condition = _object(raw, "pod condition")
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            ready = True
    try:
        return PodSnapshotV2(
            name=_text(metadata.get("name"), "pod name"),
            uid=_text(metadata.get("uid"), "pod UID"),
            resource_version=_text(metadata.get("resourceVersion"), "pod resourceVersion"),
            pod_template_hash=pod_hash,
            phase=_text(status.get("phase"), "pod phase"),
            ready=ready,
            terminating=metadata.get("deletionTimestamp") is not None,
        )
    except ValidationError:
        raise KubernetesDataError("Kubernetes Pod snapshot is invalid") from None


def _object(value: JsonValue | None, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value


def _text(value: JsonValue | None, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise KubernetesDataError(f"Kubernetes {field} is invalid")
    return value
