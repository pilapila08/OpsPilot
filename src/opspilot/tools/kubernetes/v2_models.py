"""Strict, bounded topology and metrics contracts for Kubernetes V2 Tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.integrations.kubernetes.models import NamespaceName, ResourceName
from opspilot.integrations.kubernetes.v2_models import MultiPodSnapshotV2, PodSnapshotV2

_UID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_HASH = r"^[0-9a-f]{64}$"
_QUANTITY = r"^[0-9]+(?:\.[0-9]+)?(?:n|u|m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?$"


class ServiceInputV2(StrictSchema):
    namespace: NamespaceName
    service_name: ResourceName


class ServicePortV2(StrictSchema):
    name: str | None = Field(default=None, max_length=63)
    protocol: Literal["TCP", "UDP", "SCTP"]
    port: int = Field(ge=1, le=65_535)
    target_port: int | str | None

    @field_validator("target_port")
    @classmethod
    def validate_target_port(cls, value: int | str | None) -> int | str | None:
        if isinstance(value, bool):
            raise ValueError("targetPort cannot be boolean")
        if isinstance(value, int) and not 1 <= value <= 65_535:
            raise ValueError("targetPort is outside TCP/UDP range")
        if isinstance(value, str) and not 1 <= len(value) <= 63:
            raise ValueError("named targetPort is invalid")
        return value


class ServiceOutputV2(StrictSchema):
    namespace: NamespaceName
    name: ResourceName
    uid: str = Field(min_length=1, max_length=128, pattern=_UID)
    resource_version: str = Field(min_length=1, max_length=128, pattern=_VERSION)
    selector: dict[str, str] = Field(max_length=20)
    ports: tuple[ServicePortV2, ...] = Field(max_length=32)
    service_type: Literal["ClusterIP", "NodePort", "LoadBalancer", "ExternalName"]


class EndpointsInputV2(StrictSchema):
    namespace: NamespaceName
    service_name: ResourceName


class EndpointPortV2(StrictSchema):
    name: str | None = Field(default=None, max_length=63)
    protocol: Literal["TCP", "UDP", "SCTP"]
    port: int | None = Field(default=None, ge=1, le=65_535)


class EndpointV2(StrictSchema):
    address_count: int = Field(ge=1, le=16)
    ready: bool | None
    serving: bool | None
    terminating: bool | None
    target_kind: Literal["Pod"] | None
    target_name: ResourceName | None
    target_uid: str | None = Field(default=None, max_length=128, pattern=_UID)


class EndpointSliceV2(StrictSchema):
    name: ResourceName
    uid: str = Field(min_length=1, max_length=128, pattern=_UID)
    resource_version: str = Field(min_length=1, max_length=128, pattern=_VERSION)
    address_type: Literal["IPv4", "IPv6", "FQDN"]
    ports: tuple[EndpointPortV2, ...] = Field(max_length=32)
    endpoints: tuple[EndpointV2, ...] = Field(max_length=100)


class EndpointsOutputV2(StrictSchema):
    namespace: NamespaceName
    service_name: ResourceName
    slices: tuple[EndpointSliceV2, ...] = Field(max_length=20)
    truncated: bool
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("EndpointSlice observation requires timezone")
        return value.astimezone(UTC)


class IngressInputV2(StrictSchema):
    namespace: NamespaceName
    ingress_name: ResourceName


class IngressRouteV2(StrictSchema):
    host_sha256: str | None = Field(default=None, pattern=_HASH)
    path: str = Field(min_length=1, max_length=256)
    path_type: Literal["Exact", "Prefix", "ImplementationSpecific"]
    service_name: ResourceName
    service_port: int | str


class IngressOutputV2(StrictSchema):
    namespace: NamespaceName
    name: ResourceName
    uid: str = Field(min_length=1, max_length=128, pattern=_UID)
    resource_version: str = Field(min_length=1, max_length=128, pattern=_VERSION)
    routes: tuple[IngressRouteV2, ...] = Field(max_length=100)


class ResourceUsageInputV2(StrictSchema):
    namespace: NamespaceName
    deployment_name: ResourceName
    max_pods: int = Field(default=20, ge=1, le=50)


class ContainerUsageV2(StrictSchema):
    name: str = Field(min_length=1, max_length=63)
    cpu: str = Field(min_length=1, max_length=64, pattern=_QUANTITY)
    memory: str = Field(min_length=1, max_length=64, pattern=_QUANTITY)


class PodUsageV2(StrictSchema):
    pod_name: ResourceName
    pod_uid: str = Field(min_length=1, max_length=128, pattern=_UID)
    sampled_at: datetime
    window: str = Field(min_length=1, max_length=64)
    containers: tuple[ContainerUsageV2, ...] = Field(min_length=1, max_length=100)

    @field_validator("sampled_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Metrics sample requires timezone")
        return value.astimezone(UTC)


class ResourceUsageOutputV2(StrictSchema):
    namespace: NamespaceName
    deployment_name: ResourceName
    snapshot: MultiPodSnapshotV2
    usages: tuple[PodUsageV2, ...] = Field(max_length=50)
    missing_pods: tuple[ResourceName, ...] = Field(max_length=50)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Resource observation requires timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def match_snapshot(self) -> ResourceUsageOutputV2:
        if self.namespace != self.snapshot.namespace or self.deployment_name != self.snapshot.deployment_name:
            raise ValueError("resource usage target must match Pod snapshot")
        known = {pod.name for pod in self.snapshot.pods}
        measured = {item.pod_name for item in self.usages}
        if not measured.issubset(known) or set(self.missing_pods) != known - measured:
            raise ValueError("Metrics observations must exactly partition the Pod snapshot")
        return self
