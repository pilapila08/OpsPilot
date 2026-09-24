"""V2 whitelist adds four bounded Risk 0 Kubernetes reads to the V1 baseline."""

from __future__ import annotations

from typing import Any

from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes.v2_client import KubernetesReaderV2
from opspilot.tools.kubernetes.registry import kubernetes_tool_definitions
from opspilot.tools.kubernetes.v2_handlers import KubernetesToolHandlersV2
from opspilot.tools.kubernetes.v2_models import (
    EndpointsInputV2, EndpointsOutputV2, IngressInputV2, IngressOutputV2,
    ResourceUsageInputV2, ResourceUsageOutputV2, ServiceInputV2,
    ServiceOutputV2, ServiceMembershipInputV2, ServiceMembershipOutputV2,
)
from opspilot.tools.models import RetryPolicy, ToolDefinition, ToolRiskLevel
from opspilot.tools.registry import ToolRegistry


def kubernetes_v2_tool_definitions(
    reader: KubernetesReaderV2,
) -> tuple[ToolDefinition[Any, Any], ...]:
    handlers = KubernetesToolHandlersV2(reader)
    retry = RetryPolicy(
        max_retries=1,
        retryable_errors=(
            ErrorCode.TOOL_TIMEOUT, ErrorCode.TOOL_EXECUTION_FAILED,
            ErrorCode.EXTERNAL_SERVICE_ERROR,
        ),
    )
    return (
        ToolDefinition(
            name="k8s.get_service", description="Read one scoped Service selector and ports.",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=ServiceInputV2, output_model=ServiceOutputV2,
            handler=handlers.get_service, source="kubernetes",
            timeout_seconds=10, retry_policy=retry, version="v2",
        ),
        ToolDefinition(
            name="k8s.get_service_membership",
            description="Compare one Service selector to bounded Deployment Pod labels.",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=ServiceMembershipInputV2,
            output_model=ServiceMembershipOutputV2,
            handler=handlers.get_service_membership, source="kubernetes",
            timeout_seconds=20, retry_policy=retry, version="v2",
        ),
        ToolDefinition(
            name="k8s.get_endpoints", description="Read bounded EndpointSlices for one Service.",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=EndpointsInputV2, output_model=EndpointsOutputV2,
            handler=handlers.get_endpoints, source="kubernetes",
            timeout_seconds=10, retry_policy=retry, version="v2",
        ),
        ToolDefinition(
            name="k8s.get_ingress", description="Read one Ingress route and backend snapshot.",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=IngressInputV2, output_model=IngressOutputV2,
            handler=handlers.get_ingress, source="kubernetes",
            timeout_seconds=10, retry_policy=retry, version="v2",
        ),
        ToolDefinition(
            name="k8s.get_resource_usage", description="Read bounded Deployment Pod metrics.",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=ResourceUsageInputV2, output_model=ResourceUsageOutputV2,
            handler=handlers.get_resource_usage, source="kubernetes",
            timeout_seconds=30, retry_policy=retry, version="v2",
        ),
    )


def build_kubernetes_registry_v2(reader: KubernetesReaderV2) -> ToolRegistry:
    registry = ToolRegistry()
    for definition in (*kubernetes_tool_definitions(reader), *kubernetes_v2_tool_definitions(reader)):
        registry.register(definition)
    return registry
