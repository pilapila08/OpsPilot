"""Opt-in read-only smoke for an operator-owned isolated Prometheus metric."""

from __future__ import annotations

import asyncio
import os

import pytest
from pydantic import SecretStr

from opspilot.integrations.prometheus import PrometheusHttpReader, PrometheusSettings
from opspilot.tools import ToolInvocation, ToolRegistry
from opspilot.tools.prometheus import register_prometheus_tools_v2

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.environ.get("OPSPILOT_V2_PROM_LIVE") != "1",
    reason="opt-in isolated Prometheus read smoke",
)
def test_isolated_memory_metric_read() -> None:
    required = (
        "OPSPILOT_V2_PROM_URL", "OPSPILOT_V2_PROM_NAMESPACE",
        "OPSPILOT_V2_PROM_WORKLOAD",
    )
    missing = [name for name in required if not os.environ.get(name)]
    assert not missing, f"Missing explicit Prometheus smoke settings: {', '.join(missing)}"
    token = os.environ.get("OPSPILOT_V2_PROM_TOKEN")
    settings = PrometheusSettings.model_validate({
        "base_url": os.environ["OPSPILOT_V2_PROM_URL"],
        "bearer_token": SecretStr(token) if token else None,
    })
    reader = PrometheusHttpReader(settings)

    async def check() -> None:
        registry = ToolRegistry()
        register_prometheus_tools_v2(registry, reader)
        try:
            response = await registry.invoke(ToolInvocation(
                call_id="prom_live_memory", tool="prometheus.query_memory",
                arguments={
                    "namespace": os.environ["OPSPILOT_V2_PROM_NAMESPACE"],
                    "workload_name": os.environ["OPSPILOT_V2_PROM_WORKLOAD"],
                },
            ))
            assert response.success, response.error
            assert response.data is not None
            assert response.data["status"] == "present"
        finally:
            await reader.close()

    asyncio.run(check())
