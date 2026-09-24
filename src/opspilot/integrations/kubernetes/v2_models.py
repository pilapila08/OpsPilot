"""SDK-independent multi-replica identity snapshot."""

from __future__ import annotations

from pydantic import Field, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.integrations.kubernetes.models import NamespaceName, ResourceName

_UID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class PodSnapshotV2(StrictSchema):
    name: ResourceName
    uid: str = Field(min_length=1, max_length=128, pattern=_UID)
    resource_version: str = Field(min_length=1, max_length=128, pattern=_VERSION)
    pod_template_hash: str | None = Field(default=None, max_length=63)
    phase: str = Field(min_length=1, max_length=64)
    ready: bool
    terminating: bool


class MultiPodSnapshotV2(StrictSchema):
    namespace: NamespaceName
    deployment_name: ResourceName
    deployment_uid: str = Field(min_length=1, max_length=128, pattern=_UID)
    deployment_resource_version: str = Field(
        min_length=1, max_length=128, pattern=_VERSION
    )
    deployment_generation: int = Field(ge=0)
    pods: tuple[PodSnapshotV2, ...] = Field(max_length=50)
    truncated: bool
    rollout_ambiguous: bool

    @model_validator(mode="after")
    def check_rollout(self) -> MultiPodSnapshotV2:
        hashes = {pod.pod_template_hash for pod in self.pods if pod.pod_template_hash}
        ambiguous = (
            len(hashes) > 1 or self.truncated
            or (len(self.pods) > 1 and any(
                pod.pod_template_hash is None for pod in self.pods
            ))
        )
        if self.rollout_ambiguous != ambiguous:
            raise ValueError("rollout ambiguity must reflect incomplete or mixed Pod templates")
        if len({pod.uid for pod in self.pods}) != len(self.pods):
            raise ValueError("Pod snapshot UIDs must be unique")
        return self
