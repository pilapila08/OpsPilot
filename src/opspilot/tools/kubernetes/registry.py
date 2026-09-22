"""Tool definitions and whitelist registry assembly for Kubernetes V1."""

from __future__ import annotations

from typing import Any

from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes import KubernetesReader
from opspilot.tools.kubernetes.handlers import KubernetesToolHandlers
from opspilot.tools.kubernetes.models import (
    DeploymentInput,
    DeploymentOutput,
    PodEventsInput,
    PodEventsOutput,
    PodLogsInput,
    PodLogsOutput,
    PodStatusInput,
    PodStatusOutput,
    PreviousPodLogsInput,
)
from opspilot.tools.models import (
    RetryPolicy,
    ToolDefinition,
    ToolRiskLevel,
)
from opspilot.tools.registry import ToolRegistry

_SOURCE = "kubernetes"
_VERSION = "v1"
_TIMEOUT_SECONDS = 10.0


def kubernetes_tool_definitions(
    reader: KubernetesReader,
) -> tuple[ToolDefinition[Any, Any], ...]:
    """Create the five V1 read-only definitions bound to one Reader."""

    handlers = KubernetesToolHandlers(reader)
    retry_policy = RetryPolicy(
        max_retries=1,
        initial_backoff_seconds=0.25,
        backoff_multiplier=2.0,
        max_backoff_seconds=2.0,
        retryable_errors=(
            ErrorCode.TOOL_TIMEOUT,
            ErrorCode.TOOL_EXECUTION_FAILED,
            ErrorCode.EXTERNAL_SERVICE_ERROR,
        ),
    )
    return (
        ToolDefinition(
            name="k8s.get_pod_status",
            description=(
                "Read normalized phase, conditions, and container status "
                "for one exact Pod or single-replica Deployment."
            ),
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=PodStatusInput,
            output_model=PodStatusOutput,
            handler=handlers.get_pod_status,
            source=_SOURCE,
            timeout_seconds=_TIMEOUT_SECONDS,
            retry_policy=retry_policy,
            version=_VERSION,
        ),
        ToolDefinition(
            name="k8s.get_pod_events",
            description=(
                "Read up to 100 normalized Kubernetes Events for one "
                "resolved Pod."
            ),
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=PodEventsInput,
            output_model=PodEventsOutput,
            handler=handlers.get_pod_events,
            source=_SOURCE,
            timeout_seconds=_TIMEOUT_SECONDS,
            retry_policy=retry_policy,
            version=_VERSION,
        ),
        ToolDefinition(
            name="k8s.get_pod_logs",
            description=(
                "Read bounded current logs for one validated Pod container."
            ),
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=PodLogsInput,
            output_model=PodLogsOutput,
            handler=handlers.get_pod_logs,
            source=_SOURCE,
            timeout_seconds=_TIMEOUT_SECONDS,
            retry_policy=retry_policy,
            version=_VERSION,
        ),
        ToolDefinition(
            name="k8s.get_previous_logs",
            description=(
                "Read bounded logs from the previous instance of one "
                "validated Pod container."
            ),
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=PreviousPodLogsInput,
            output_model=PodLogsOutput,
            handler=handlers.get_previous_logs,
            source=_SOURCE,
            timeout_seconds=_TIMEOUT_SECONDS,
            retry_policy=retry_policy,
            version=_VERSION,
        ),
        ToolDefinition(
            name="k8s.get_deployment",
            description=(
                "Read normalized Deployment replicas, selector, containers, "
                "probes, resources, and redacted environment metadata."
            ),
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=DeploymentInput,
            output_model=DeploymentOutput,
            handler=handlers.get_deployment,
            source=_SOURCE,
            timeout_seconds=_TIMEOUT_SECONDS,
            retry_policy=retry_policy,
            version=_VERSION,
        ),
    )


def build_kubernetes_registry(reader: KubernetesReader) -> ToolRegistry:
    """Build a whitelist containing exactly the five Kubernetes V1 tools."""

    registry = ToolRegistry()
    for definition in kubernetes_tool_definitions(reader):
        registry.register(definition)
    return registry
