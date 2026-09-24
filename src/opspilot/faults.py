"""Shared V2 fault-family and target-kind vocabulary."""

from typing import Literal, TypeAlias

FaultFamilyV2: TypeAlias = Literal[
    "crashloop_backoff",
    "liveness_probe_failed",
    "readiness_probe_failed",
    "oom_killed",
    "image_pull_backoff",
    "service_503",
    "latency_increase",
    "post_deployment_failure",
]
FaultCandidateV2: TypeAlias = Literal[
    "crashloop_backoff",
    "liveness_probe_failed",
    "readiness_probe_failed",
    "oom_killed",
    "image_pull_backoff",
    "service_503",
    "latency_increase",
    "post_deployment_failure",
    "unknown",
]
TargetKindV2: TypeAlias = Literal["pod", "deployment", "service", "ingress"]
TargetKindCandidateV2: TypeAlias = Literal[
    "pod", "deployment", "service", "ingress", "unknown"
]
