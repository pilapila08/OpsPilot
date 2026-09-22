"""Strict input and output contracts for the five V1 Kubernetes tools."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.integrations.kubernetes.models import (
    ContainerName,
    NamespaceName,
    PodLogQuery,
    PodTarget,
    ResourceName,
)

MAX_EVENT_MESSAGE_CHARS = 2_048
MAX_LOG_BYTES = 65_536

ShortText = Annotated[str, Field(min_length=1, max_length=253)]
MessageText = Annotated[str, Field(max_length=MAX_EVENT_MESSAGE_CHARS)]
TimestampText = Annotated[str, Field(min_length=1, max_length=64)]
EnvironmentName = Annotated[
    str,
    Field(min_length=1, max_length=253, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
ResourceQuantity = Annotated[str, Field(min_length=1, max_length=128)]


class _PodTargetInput(StrictSchema):
    namespace: NamespaceName
    pod_name: ResourceName | None = None
    workload_name: ResourceName | None = None

    @model_validator(mode="after")
    def require_exactly_one_resource(self) -> _PodTargetInput:
        if (self.pod_name is None) == (self.workload_name is None):
            raise ValueError(
                "exactly one of pod_name or workload_name must be provided"
            )
        return self

    def to_target(self, *, container_name: str | None = None) -> PodTarget:
        return PodTarget(
            namespace=self.namespace,
            pod_name=self.pod_name,
            workload_name=self.workload_name,
            container_name=container_name,
        )


class PodStatusInput(_PodTargetInput):
    """Input for k8s.get_pod_status."""


class PodEventsInput(_PodTargetInput):
    """Input for k8s.get_pod_events."""


class _PodLogsInput(_PodTargetInput):
    container_name: ContainerName | None = None
    tail_lines: int = Field(default=200, ge=1, le=1_000)
    since_seconds: int | None = Field(default=None, ge=1, le=604_800)
    max_bytes: int = Field(default=MAX_LOG_BYTES, ge=1_024, le=MAX_LOG_BYTES)

    def query(self) -> PodLogQuery:
        return PodLogQuery(
            tail_lines=self.tail_lines,
            since_seconds=self.since_seconds,
            max_bytes=self.max_bytes,
        )

    def target(self) -> PodTarget:
        return self.to_target(container_name=self.container_name)


class PodLogsInput(_PodLogsInput):
    """Input for k8s.get_pod_logs."""


class PreviousPodLogsInput(_PodLogsInput):
    """Input for k8s.get_previous_logs."""


class DeploymentInput(StrictSchema):
    """Input for k8s.get_deployment."""

    namespace: NamespaceName
    deployment_name: ResourceName


class PodConditionOutput(StrictSchema):
    type: ShortText
    status: ShortText
    reason: ShortText | None = None
    message: MessageText | None = None
    last_transition_time: TimestampText | None = None


class ContainerStatusOutput(StrictSchema):
    name: ContainerName
    ready: bool
    restart_count: int = Field(ge=0)
    state: Literal["waiting", "running", "terminated", "unknown"]
    reason: ShortText | None = None
    last_exit_code: int | None = None
    last_reason: ShortText | None = None
    last_started_at: TimestampText | None = None
    last_finished_at: TimestampText | None = None


class PodStatusOutput(StrictSchema):
    namespace: NamespaceName
    pod_name: ResourceName
    phase: ShortText
    conditions: tuple[PodConditionOutput, ...] = Field(max_length=100)
    containers: tuple[ContainerStatusOutput, ...] = Field(max_length=100)


class KubernetesEventOutput(StrictSchema):
    name: ShortText
    type: ShortText
    reason: ShortText
    message: MessageText
    count: int = Field(ge=0)
    first_timestamp: TimestampText | None = None
    last_timestamp: TimestampText | None = None


class PodEventsOutput(StrictSchema):
    namespace: NamespaceName
    pod_name: ResourceName
    events: tuple[KubernetesEventOutput, ...] = Field(max_length=100)


class PodLogsOutput(StrictSchema):
    namespace: NamespaceName
    pod_name: ResourceName
    container: ContainerName
    previous: bool
    content: str = Field(max_length=MAX_LOG_BYTES)
    truncated: bool
    byte_count: int = Field(ge=0, le=MAX_LOG_BYTES)


class ProbeOutput(StrictSchema):
    kind: Literal["http", "tcp", "exec"]
    path: str | None = Field(default=None, min_length=1, max_length=2_048)
    port: int | ShortText | None = None
    host: str | None = Field(default=None, max_length=253)
    scheme: ShortText | None = None
    command: tuple[str, ...] = Field(default=(), max_length=100)
    initial_delay_seconds: int = Field(default=0, ge=0)
    period_seconds: int = Field(default=10, ge=1)
    timeout_seconds: int = Field(default=1, ge=1)
    failure_threshold: int = Field(default=3, ge=1)
    success_threshold: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_target(self) -> ProbeOutput:
        if self.kind == "http" and (self.path is None or self.port is None):
            raise ValueError("http probe requires path and port")
        if self.kind == "tcp" and self.port is None:
            raise ValueError("tcp probe requires port")
        if self.kind == "exec" and not self.command:
            raise ValueError("exec probe requires command")
        return self


class ResourceRequirementsOutput(StrictSchema):
    requests: dict[str, ResourceQuantity] = Field(default_factory=dict)
    limits: dict[str, ResourceQuantity] = Field(default_factory=dict)


class EnvironmentOutput(StrictSchema):
    name: EnvironmentName
    kind: Literal[
        "literal",
        "field_ref",
        "resource_field_ref",
        "config_map_key_ref",
        "secret_key_ref",
        "unknown_ref",
    ]
    value: str | None = Field(default=None, max_length=2_048)
    reference_name: str | None = Field(default=None, max_length=253)
    reference_key: str | None = Field(default=None, max_length=253)
    optional: bool | None = None
    redacted: bool = False

    @model_validator(mode="after")
    def protect_redacted_values(self) -> EnvironmentOutput:
        if self.redacted and self.value is not None:
            raise ValueError("redacted environment values cannot be included")
        if self.kind == "secret_key_ref" and not self.redacted:
            raise ValueError("secret references must be marked redacted")
        if self.kind == "literal" and not self.redacted and self.value is None:
            raise ValueError("non-redacted literal environment requires a value")
        return self


class DeploymentContainerOutput(StrictSchema):
    name: ContainerName
    image: str = Field(min_length=1, max_length=2_048)
    liveness_probe: ProbeOutput | None = None
    readiness_probe: ProbeOutput | None = None
    startup_probe: ProbeOutput | None = None
    resources: ResourceRequirementsOutput
    environment: tuple[EnvironmentOutput, ...] = Field(max_length=500)


class DeploymentOutput(StrictSchema):
    namespace: NamespaceName
    name: ResourceName
    generation: int = Field(ge=0)
    replicas: int = Field(ge=0)
    ready_replicas: int = Field(ge=0)
    unavailable_replicas: int = Field(ge=0)
    selector: dict[str, str]
    containers: tuple[DeploymentContainerOutput, ...] = Field(
        min_length=1,
        max_length=100,
    )
