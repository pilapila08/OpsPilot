"""Opt-in smoke against an operator-prepared, isolated fixture namespace."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from opspilot.api import create_app
from opspilot.api.settings import ApiSettings
from opspilot.integrations.kubernetes import build_kubernetes_reader
from opspilot.integrations.kubernetes.models import KubernetesClientSettings
from opspilot.tools import ToolInvocation
from opspilot.tools.kubernetes import build_kubernetes_registry

pytestmark = pytest.mark.live


@pytest.mark.skipif(os.environ.get("OPSPILOT_LIVE_SMOKE") != "1", reason="opt-in cluster smoke")
def test_readonly_tools_and_live_diagnosis(tmp_path: Path) -> None:
    settings = ApiSettings.from_env().model_copy(update={
        "database_url": f"sqlite+pysqlite:///{(tmp_path / 'live.db').as_posix()}",
    })
    assert settings.live_enabled
    reader = build_kubernetes_reader(KubernetesClientSettings(
        mode=settings.kube_mode, kubeconfig_path=settings.kubeconfig_path,
        context=settings.kube_context,
    ))
    namespace = "opspilot-fixtures"
    workload = "slow-start-api"

    async def check_tools() -> None:
        registry = build_kubernetes_registry(reader)
        pod: dict[str, JsonValue] = {"namespace": namespace, "workload_name": workload}
        calls: tuple[tuple[str, dict[str, JsonValue]], ...] = (
            ("k8s.get_pod_status", pod),
            ("k8s.get_pod_events", pod),
            ("k8s.get_pod_logs", {**pod, "container_name": workload}),
            ("k8s.get_previous_logs", {**pod, "container_name": workload}),
            ("k8s.get_deployment", {"namespace": namespace, "deployment_name": workload}),
        )
        for index, (tool, arguments) in enumerate(calls, 1):
            response = await registry.invoke(ToolInvocation(
                call_id=f"smoke_{index}", tool=tool, arguments=arguments,
            ))
            assert response.success, (tool, response.error)

    try:
        asyncio.run(check_tools())
    finally:
        reader.close()
    with TestClient(create_app(settings)) as client:
        created = client.post("/diagnosis", json={
            "query": "Why is slow-start-api restarting?",
            "namespace": namespace, "mode": "live",
        })
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        deadline = time.monotonic() + settings.budget.timeout_seconds + 10
        while time.monotonic() < deadline:
            output = client.get(f"/diagnosis/{task_id}").json()
            if output["status"] in {
                "COMPLETED", "PARTIAL", "FAILED", "BUDGET_EXCEEDED", "POLICY_REJECTED",
            }:
                break
            time.sleep(1)
        assert output["status"] == "COMPLETED", output
        assert {item["source"] for item in output["evidence"]} >= {
            "kubernetes_status", "kubernetes_events",
            "kubernetes_logs", "kubernetes_deployment",
        }
        assert output["verification"]["supported"] is True
