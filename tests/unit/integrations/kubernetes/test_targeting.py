import asyncio

import pytest
from pydantic import JsonValue

from opspilot.integrations.kubernetes import (
    JsonObject,
    KubernetesAmbiguousTargetError,
    KubernetesDataError,
    KubernetesNotFoundError,
    PodTarget,
    PodTargetResolver,
)


def _pod(
    name: str,
    *,
    namespace: str = "team-a",
    uid: str = "pod-uid",
    containers: tuple[str, ...] = ("api",),
    terminating: bool = False,
) -> JsonObject:
    metadata: JsonObject = {
        "namespace": namespace,
        "name": name,
        "uid": uid,
    }
    if terminating:
        metadata["deletionTimestamp"] = "2026-09-22T10:00:00Z"
    return {
        "metadata": metadata,
        "spec": {
            "containers": [{"name": container} for container in containers],
        },
    }


def _deployment(
    *,
    namespace: str = "team-a",
    name: str = "api",
    labels: JsonObject | None = None,
    match_expressions: list[JsonValue] | None = None,
) -> JsonObject:
    selector: JsonObject = {
        "matchLabels": labels
        if labels is not None
        else {"app.kubernetes.io/name": "api", "tier": "backend"},
    }
    if match_expressions is not None:
        selector["matchExpressions"] = match_expressions
    return {
        "metadata": {
            "namespace": namespace,
            "name": name,
        },
        "spec": {
            "selector": selector,
        },
    }


class FakeReader:
    def __init__(
        self,
        *,
        exact_pod: JsonObject | None = None,
        deployment: JsonObject | None = None,
        listed_pods: tuple[JsonObject, ...] = (),
    ) -> None:
        self.exact_pod = exact_pod
        self.deployment = deployment
        self.listed_pods = listed_pods
        self.last_selector: str | None = None

    async def read_pod(self, *, namespace: str, pod_name: str) -> JsonObject:
        del namespace, pod_name
        if self.exact_pod is None:
            raise KubernetesNotFoundError("Pod was not found")
        return self.exact_pod

    async def list_pods(
        self,
        *,
        namespace: str,
        label_selector: str,
    ) -> tuple[JsonObject, ...]:
        del namespace
        self.last_selector = label_selector
        return self.listed_pods

    async def list_events(
        self,
        *,
        namespace: str,
        field_selector: str,
        limit: int,
    ) -> tuple[JsonObject, ...]:
        del namespace, field_selector, limit
        return ()

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
        return ""

    async def read_deployment(
        self,
        *,
        namespace: str,
        deployment_name: str,
    ) -> JsonObject:
        del namespace, deployment_name
        if self.deployment is None:
            raise KubernetesNotFoundError("Deployment was not found")
        return self.deployment


def test_resolves_exact_pod_and_requested_container() -> None:
    reader = FakeReader(exact_pod=_pod("api-7d9f", containers=("api", "sidecar")))
    resolver = PodTargetResolver(reader)

    resolved = asyncio.run(
        resolver.resolve(
            PodTarget(
                namespace="team-a",
                pod_name="api-7d9f",
                container_name="sidecar",
            )
        )
    )

    assert resolved.pod_name == "api-7d9f"
    assert resolved.source == "pod"
    assert resolved.requested_container == "sidecar"


def test_resolves_single_active_workload_pod_with_stable_selector() -> None:
    reader = FakeReader(
        deployment=_deployment(),
        listed_pods=(
            _pod("api-old", terminating=True),
            _pod("api-current", uid="current-uid"),
        ),
    )
    resolver = PodTargetResolver(reader)

    resolved = asyncio.run(
        resolver.resolve(PodTarget(namespace="team-a", workload_name="api"))
    )

    assert resolved.pod_name == "api-current"
    assert resolved.source == "deployment"
    assert resolved.workload_name == "api"
    assert (
        reader.last_selector
        == "app.kubernetes.io/name=api,tier=backend"
    )


def test_workload_resolution_rejects_zero_active_pods() -> None:
    reader = FakeReader(
        deployment=_deployment(),
        listed_pods=(_pod("api-old", terminating=True),),
    )

    with pytest.raises(KubernetesNotFoundError, match="No active pod"):
        asyncio.run(
            PodTargetResolver(reader).resolve(
                PodTarget(namespace="team-a", workload_name="api")
            )
        )


def test_workload_resolution_rejects_multiple_active_pods() -> None:
    reader = FakeReader(
        deployment=_deployment(),
        listed_pods=(_pod("api-a"), _pod("api-b", uid="pod-b")),
    )

    with pytest.raises(KubernetesAmbiguousTargetError, match="Multiple active"):
        asyncio.run(
            PodTargetResolver(reader).resolve(
                PodTarget(namespace="team-a", workload_name="api")
            )
        )


def test_workload_resolution_rejects_invalid_deployment_selector() -> None:
    reader = FakeReader(
        deployment=_deployment(labels={"unsafe,key": "api"}),
        listed_pods=(_pod("api-a"),),
    )

    with pytest.raises(KubernetesDataError, match="selector is invalid"):
        asyncio.run(
            PodTargetResolver(reader).resolve(
                PodTarget(namespace="team-a", workload_name="api")
            )
        )


def test_workload_resolution_rejects_match_expressions() -> None:
    reader = FakeReader(
        deployment=_deployment(
            match_expressions=[
                {
                    "key": "tier",
                    "operator": "In",
                    "values": ["backend"],
                }
            ]
        ),
        listed_pods=(_pod("api-a"),),
    )

    with pytest.raises(KubernetesDataError, match="match expressions"):
        asyncio.run(
            PodTargetResolver(reader).resolve(
                PodTarget(namespace="team-a", workload_name="api")
            )
        )


def test_resolution_rejects_missing_requested_container() -> None:
    reader = FakeReader(exact_pod=_pod("api-7d9f"))

    with pytest.raises(KubernetesNotFoundError, match="container"):
        asyncio.run(
            PodTargetResolver(reader).resolve(
                PodTarget(
                    namespace="team-a",
                    pod_name="api-7d9f",
                    container_name="sidecar",
                )
            )
        )


def test_resolution_rejects_namespace_mismatch_from_api() -> None:
    reader = FakeReader(exact_pod=_pod("api-7d9f", namespace="other"))

    with pytest.raises(KubernetesDataError, match="namespace did not match"):
        asyncio.run(
            PodTargetResolver(reader).resolve(
                PodTarget(namespace="team-a", pod_name="api-7d9f")
            )
        )
