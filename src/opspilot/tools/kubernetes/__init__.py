"""Public contracts and registry assembly for Kubernetes V1 tools."""

from opspilot.tools.kubernetes.handlers import KubernetesToolHandlers
from opspilot.tools.kubernetes.models import (
    ContainerStatusOutput,
    DeploymentContainerOutput,
    DeploymentInput,
    DeploymentOutput,
    EnvironmentOutput,
    KubernetesEventOutput,
    PodConditionOutput,
    PodEventsInput,
    PodEventsOutput,
    PodLogsInput,
    PodLogsOutput,
    PodStatusInput,
    PodStatusOutput,
    PreviousPodLogsInput,
    ProbeOutput,
    ResourceRequirementsOutput,
)
from opspilot.tools.kubernetes.registry import (
    build_kubernetes_registry,
    kubernetes_tool_definitions,
)

__all__ = [
    "ContainerStatusOutput",
    "DeploymentContainerOutput",
    "DeploymentInput",
    "DeploymentOutput",
    "EnvironmentOutput",
    "KubernetesEventOutput",
    "KubernetesToolHandlers",
    "PodConditionOutput",
    "PodEventsInput",
    "PodEventsOutput",
    "PodLogsInput",
    "PodLogsOutput",
    "PodStatusInput",
    "PodStatusOutput",
    "PreviousPodLogsInput",
    "ProbeOutput",
    "ResourceRequirementsOutput",
    "build_kubernetes_registry",
    "kubernetes_tool_definitions",
]
