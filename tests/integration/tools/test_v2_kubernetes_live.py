"""Operator-opted read-only V2 Kubernetes Tool smoke; no fixture writes."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from opspilot.integrations.kubernetes import (
    KubernetesClientSettings, KubernetesConnectionMode,
    build_kubernetes_reader_v2,
)
from opspilot.tools import ToolInvocation
from opspilot.tools.kubernetes import build_kubernetes_registry_v2

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.environ.get("OPSPILOT_V2_K8S_LIVE") != "1",
    reason="opt-in V2 read-only cluster smoke",
)
def test_v2_readonly_topology_and_metrics() -> None:
    required = (
        "OPSPILOT_V2_KUBECONFIG", "OPSPILOT_V2_CONTEXT",
        "OPSPILOT_V2_NAMESPACE", "OPSPILOT_V2_SERVICE",
        "OPSPILOT_V2_INGRESS", "OPSPILOT_V2_DEPLOYMENT",
    )
    missing = [key for key in required if not os.environ.get(key)]
    assert not missing, f"Missing explicit V2 live settings: {', '.join(missing)}"
    reader = build_kubernetes_reader_v2(KubernetesClientSettings(
        mode=KubernetesConnectionMode.KUBECONFIG,
        kubeconfig_path=Path(os.environ["OPSPILOT_V2_KUBECONFIG"]),
        context=os.environ["OPSPILOT_V2_CONTEXT"],
    ))
    namespace = os.environ["OPSPILOT_V2_NAMESPACE"]
    targets = (
        ("k8s.get_service", "service_name", os.environ["OPSPILOT_V2_SERVICE"]),
        ("k8s.get_endpoints", "service_name", os.environ["OPSPILOT_V2_SERVICE"]),
        ("k8s.get_ingress", "ingress_name", os.environ["OPSPILOT_V2_INGRESS"]),
        ("k8s.get_resource_usage", "deployment_name", os.environ["OPSPILOT_V2_DEPLOYMENT"]),
    )

    async def check() -> None:
        registry = build_kubernetes_registry_v2(reader)
        for index, (tool, key, target) in enumerate(targets, 1):
            response = await registry.invoke(ToolInvocation(
                call_id=f"v2_live_{index}", tool=tool,
                arguments={"namespace": namespace, key: target},
            ))
            assert response.success, (tool, response.error)

    try:
        asyncio.run(check())
    finally:
        reader.close()
