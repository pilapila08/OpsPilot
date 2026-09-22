"""Strict, SDK-independent contracts for Kubernetes reads."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, model_validator

from opspilot.agent.schemas import StrictSchema

JsonObject: TypeAlias = dict[str, JsonValue]

_DNS_LABEL_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_DNS_SUBDOMAIN_PATTERN = (
    r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)
_CONTEXT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,252}$"

NamespaceName = Annotated[
    str,
    Field(min_length=1, max_length=63, pattern=_DNS_LABEL_PATTERN),
]
ResourceName = Annotated[
    str,
    Field(min_length=1, max_length=253, pattern=_DNS_SUBDOMAIN_PATTERN),
]
ContainerName = Annotated[
    str,
    Field(min_length=1, max_length=63, pattern=_DNS_LABEL_PATTERN),
]


class KubernetesConnectionMode(StrEnum):
    IN_CLUSTER = "in_cluster"
    KUBECONFIG = "kubeconfig"


class KubernetesClientSettings(StrictSchema):
    """Explicit connection settings; kubeconfig discovery is never implicit."""

    mode: KubernetesConnectionMode = KubernetesConnectionMode.IN_CLUSTER
    kubeconfig_path: Path | None = None
    context: str | None = Field(
        default=None,
        min_length=1,
        max_length=253,
        pattern=_CONTEXT_PATTERN,
    )
    request_timeout_seconds: float = Field(default=8.0, gt=0, le=30)

    @model_validator(mode="after")
    def validate_mode_fields(self) -> KubernetesClientSettings:
        if self.mode is KubernetesConnectionMode.IN_CLUSTER:
            if self.kubeconfig_path is not None or self.context is not None:
                raise ValueError(
                    "in-cluster mode cannot include kubeconfig path or context"
                )
            return self

        if self.kubeconfig_path is None or self.context is None:
            raise ValueError(
                "kubeconfig mode requires an explicit path and context"
            )
        return self


class PodTarget(StrictSchema):
    """A pod target expressed by exact pod or single-replica Deployment."""

    namespace: NamespaceName
    pod_name: ResourceName | None = None
    workload_name: ResourceName | None = None
    container_name: ContainerName | None = None

    @model_validator(mode="after")
    def require_exactly_one_resource(self) -> PodTarget:
        if (self.pod_name is None) == (self.workload_name is None):
            raise ValueError(
                "exactly one of pod_name or workload_name must be provided"
            )
        return self


class PodLogQuery(StrictSchema):
    """Bounded options shared by current and previous log reads."""

    tail_lines: int = Field(default=200, ge=1, le=1_000)
    since_seconds: int | None = Field(default=None, ge=1, le=604_800)
    max_bytes: int = Field(default=65_536, ge=1_024, le=65_536)


class ResolvedPod(StrictSchema):
    """A concrete pod selected by deterministic target resolution."""

    namespace: NamespaceName
    pod_name: ResourceName
    pod_uid: str = Field(min_length=1, max_length=128)
    container_names: tuple[ContainerName, ...] = Field(min_length=1, max_length=100)
    requested_container: ContainerName | None = None
    source: Literal["pod", "deployment"]
    workload_name: ResourceName | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> ResolvedPod:
        if self.source == "pod" and self.workload_name is not None:
            raise ValueError("exact pod resolution cannot include workload_name")
        if self.source == "deployment" and self.workload_name is None:
            raise ValueError("deployment resolution requires workload_name")
        if (
            self.requested_container is not None
            and self.requested_container not in self.container_names
        ):
            raise ValueError("requested container is not present in the pod")
        return self
