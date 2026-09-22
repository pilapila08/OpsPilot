"""Read-only Kubernetes client boundary."""

from opspilot.integrations.kubernetes.client import (
    KubernetesReader,
    KubernetesSdkReader,
    build_kubernetes_reader,
)
from opspilot.integrations.kubernetes.errors import (
    KubernetesAmbiguousTargetError,
    KubernetesConfigurationError,
    KubernetesDataError,
    KubernetesIntegrationError,
    KubernetesNotFoundError,
    KubernetesPermissionError,
    KubernetesTimeoutError,
    KubernetesTransientError,
)
from opspilot.integrations.kubernetes.models import (
    ContainerName,
    JsonObject,
    KubernetesClientSettings,
    KubernetesConnectionMode,
    NamespaceName,
    PodLogQuery,
    PodTarget,
    ResolvedPod,
    ResourceName,
)
from opspilot.integrations.kubernetes.targeting import PodTargetResolver

__all__ = [
    "ContainerName",
    "JsonObject",
    "KubernetesAmbiguousTargetError",
    "KubernetesClientSettings",
    "KubernetesConfigurationError",
    "KubernetesConnectionMode",
    "KubernetesDataError",
    "KubernetesIntegrationError",
    "KubernetesNotFoundError",
    "KubernetesPermissionError",
    "KubernetesReader",
    "KubernetesSdkReader",
    "KubernetesTimeoutError",
    "KubernetesTransientError",
    "NamespaceName",
    "PodLogQuery",
    "PodTarget",
    "PodTargetResolver",
    "ResolvedPod",
    "ResourceName",
    "build_kubernetes_reader",
]
