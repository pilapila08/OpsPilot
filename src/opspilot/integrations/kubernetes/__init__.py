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
    KubernetesInvalidRequestError,
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
from opspilot.integrations.kubernetes.targeting_v2 import MultiPodResolverV2
from opspilot.integrations.kubernetes.v2_client import (
    KubernetesReaderV2,
    KubernetesSdkReaderV2,
    build_kubernetes_reader_v2,
)
from opspilot.integrations.kubernetes.v2_models import MultiPodSnapshotV2, PodSnapshotV2

__all__ = [
    "ContainerName",
    "JsonObject",
    "KubernetesAmbiguousTargetError",
    "KubernetesClientSettings",
    "KubernetesConfigurationError",
    "KubernetesConnectionMode",
    "KubernetesDataError",
    "KubernetesIntegrationError",
    "KubernetesInvalidRequestError",
    "KubernetesNotFoundError",
    "KubernetesPermissionError",
    "KubernetesReader",
    "KubernetesSdkReader",
    "KubernetesReaderV2",
    "KubernetesSdkReaderV2",
    "KubernetesTimeoutError",
    "KubernetesTransientError",
    "NamespaceName",
    "PodLogQuery",
    "PodTarget",
    "PodTargetResolver",
    "MultiPodResolverV2",
    "MultiPodSnapshotV2",
    "PodSnapshotV2",
    "ResolvedPod",
    "ResourceName",
    "build_kubernetes_reader",
    "build_kubernetes_reader_v2",
]
