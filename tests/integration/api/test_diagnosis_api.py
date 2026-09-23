from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import importlib
import json
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot.agent.state import AgentStatus
from opspilot.api import create_app
from opspilot.api.settings import ApiSettings
from opspilot.cases import LoadedCase, load_case
from opspilot.llm.errors import ModelExternalError
from opspilot.llm.openai_adapter import OpenAIStructuredModelClient
from opspilot.llm.strict_wire import StrictJsonEnvelope
from opspilot.routing.models import RouterModelOutput
from opspilot.runtime import ReplayRegistryFactory
from opspilot.storage import AgentRunRecord, LLMCallRecord, PromptVersionRecord, ToolCallRecord
from tests.integration.tools.test_kubernetes_registry import CaseReader

REQUEST = {
    "query": "Why is slow-start-api restarting?",
    "namespace": "opspilot-fixtures",
    "mode": "replay",
    "case_id": "crashloop_liveness_v1",
}


def _client(tmp_path: Path, **changes: object) -> TestClient:
    settings = ApiSettings.model_validate({
        "database_url": f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}",
        **changes,
    })
    return TestClient(create_app(settings))


def test_replay_post_get_persists_audited_diagnosis(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/diagnosis", json=REQUEST)
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        assert created.json()["status"] == "CREATED"
        for _ in range(20):
            output = client.get(f"/diagnosis/{task_id}")
            if output.json()["status"] == "COMPLETED":
                break
        assert output.status_code == 200
        payload = output.json()
        assert payload["status"] == "COMPLETED", payload
        assert "40 seconds" in payload["root_cause"]
        assert payload["recommendation"]
        assert payload["verification"]["supported"] is True
        assert {item["source"] for item in payload["evidence"]} == {
            "kubernetes_status", "kubernetes_events",
            "kubernetes_logs", "kubernetes_deployment",
        }
        assert "prompt" not in str(payload).lower()
        assert "configured_startup_delay_seconds" not in str(payload)
        database_url = f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}"
        with Session(create_engine(database_url)) as session:
            run_id = session.scalar(select(AgentRunRecord.id).where(AgentRunRecord.task_id == task_id))
            assert run_id is not None
            assert len(session.scalars(select(LLMCallRecord).where(LLMCallRecord.run_id == run_id)).all()) == 3
            assert len(session.scalars(select(ToolCallRecord).where(ToolCallRecord.run_id == run_id)).all()) == 4


def test_idempotency_and_input_errors(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = {"Idempotency-Key": "case-one"}
        first = client.post("/diagnosis", json=REQUEST, headers=headers)
        second = client.post("/diagnosis", json=REQUEST, headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["task_id"] == second.json()["task_id"]
        changed = client.post("/diagnosis", json={**REQUEST, "query": "different"}, headers=headers)
        assert changed.status_code == 409
        assert client.get("/diagnosis/task_missing").status_code == 404
        for body in (
            {**REQUEST, "query": ""},
            {**REQUEST, "namespace": "bad/ns"},
            {**REQUEST, "mode": "wrong"},
            {**REQUEST, "case_id": "unknown_case"},
            {**REQUEST, "mode": "live", "case_id": None},
            {**REQUEST, "case_id": None},
        ):
            response = client.post("/diagnosis", json=body)
            assert response.status_code in {422, 503}, response.text
        assert client.post("/diagnosis", json=REQUEST, headers={"Idempotency-Key": "bad key"}).status_code == 422


def test_budget_exhaustion_is_stable_http_result(tmp_path: Path) -> None:
    from opspilot.agent.schemas import BudgetLimits

    with _client(tmp_path, budget=BudgetLimits(max_tool_calls=1)) as client:
        created = client.post("/diagnosis", json=REQUEST)
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        for _ in range(20):
            output = client.get(f"/diagnosis/{task_id}").json()
            if output["status"] == AgentStatus.BUDGET_EXCEEDED.value:
                break
        assert output["status"] == "BUDGET_EXCEEDED"
        assert output["error"]["code"] in {"BUDGET_EXCEEDED", "RUNTIME_FAILED"}


def test_live_configuration_does_not_silently_fallback(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/diagnosis", json={**REQUEST, "mode": "live", "case_id": None},
        )
        assert response.status_code == 503
        assert "disabled" in response.json()["detail"]


def test_policy_rejection_has_stable_http_result(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            "/diagnosis", json={**REQUEST, "namespace": "other-namespace"},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        for _ in range(20):
            output = client.get(f"/diagnosis/{task_id}").json()
            if output["status"] == "POLICY_REJECTED":
                break
        assert output["status"] == "POLICY_REJECTED"
        assert output["root_cause"] is None
        assert output["error"]["code"] == "POLICY_REJECTED"


def test_model_failure_has_stable_http_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("opspilot.api.app")

    async def unavailable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ModelExternalError("provider secret and stack must not be exposed")

    monkeypatch.setattr(app_module.CaseModelClient, "complete", unavailable)
    with _client(tmp_path) as client:
        created = client.post("/diagnosis", json=REQUEST)
        task_id = created.json()["task_id"]
        for _ in range(20):
            output = client.get(f"/diagnosis/{task_id}").json()
            if output["status"] == "FAILED":
                break
        assert output["status"] == "FAILED"
        assert "secret" not in str(output).lower()
        assert output["error"]["code"] == "EXTERNAL_SERVICE_ERROR"


def test_missing_signal_produces_partial_http_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("opspilot.api.app")

    def without_logs(path: Path) -> LoadedCase:
        case = load_case(path)
        responses = tuple(
            response.model_copy(update={"data": {**response.data, "content": ""}})
            if response.metadata.tool_name == "k8s.get_previous_logs" and response.data is not None
            else response
            for response in case.responses
        )
        return replace(case, responses=responses)

    monkeypatch.setattr(
        app_module, "ReplayRegistryFactory",
        lambda cases: ReplayRegistryFactory(cases, loader=without_logs),
    )
    with _client(tmp_path) as client:
        created = client.post("/diagnosis", json=REQUEST)
        task_id = created.json()["task_id"]
        for _ in range(20):
            output = client.get(f"/diagnosis/{task_id}").json()
            if output["status"] == "PARTIAL":
                break
        assert output["status"] == "PARTIAL"
        assert output["verification"]["supported"] is False
        assert output["root_cause"]


def test_live_api_uses_flat_wire_and_v2_prompts_with_same_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("opspilot.api.app")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(CaseReader, "close", lambda self: None, raising=False)
    monkeypatch.setattr(app_module, "build_kubernetes_reader", lambda _: CaseReader())

    class WireResponses:
        async def parse(self, **kwargs: Any) -> object:
            target = kwargs["text_format"]
            user_data = json.loads(kwargs["input"][1]["content"])
            if target is RouterModelOutput:
                payload = {
                    "intent": "diagnose", "domain": "kubernetes",
                    "problem_type": "pod_restart", "resource": "slow-start-api",
                }
            else:
                assert target is StrictJsonEnvelope
                if "tool_descriptors" in user_data:
                    pod = {"namespace": "opspilot-fixtures", "workload_name": "slow-start-api"}
                    plan = {
                        "schema_version": 1,
                        "steps": [
                            {"step_id": 1, "call_id": "call_status", "tool": "k8s.get_pod_status", "arguments": pod, "reason": "Read status"},
                            {"step_id": 2, "call_id": "call_events", "tool": "k8s.get_pod_events", "arguments": pod, "reason": "Read events"},
                            {"step_id": 3, "call_id": "call_previous", "tool": "k8s.get_previous_logs", "arguments": {**pod, "container_name": "slow-start-api"}, "reason": "Read previous logs"},
                            {"step_id": 4, "call_id": "call_deployment", "tool": "k8s.get_deployment", "arguments": {"namespace": "opspilot-fixtures", "deployment_name": "slow-start-api"}, "reason": "Read probe"},
                        ],
                    }
                    payload = {"payload_json": json.dumps(plan)}
                else:
                    evidence_ids = [item["evidence_id"] for item in user_data["evidence"]]
                    payload = {"payload_json": json.dumps({
                        "schema_version": 1,
                        "root_cause": "Early liveness probe",
                        "recommendation": "Add a startup probe",
                        "claims": [{
                            "claim_id": "claim_live_001",
                            "text": "Liveness interrupts startup",
                            "evidence_ids": evidence_ids,
                            "inference_confidence": 0.5,
                        }],
                    })}
            return SimpleNamespace(
                output_parsed=payload,
                usage=SimpleNamespace(input_tokens=10, output_tokens=10),
                id="response_test", model="test-model",
            )

    monkeypatch.setattr(
        app_module, "build_openai_structured_client",
        lambda _: OpenAIStructuredModelClient(WireResponses()),
    )
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'live-fake.db').as_posix()}"
    settings = ApiSettings(
        database_url=database_url, live_enabled=True, model_name="test-model",
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/diagnosis", json={**REQUEST, "mode": "live", "case_id": None},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        for _ in range(20):
            output = client.get(f"/diagnosis/{task_id}").json()
            if output["status"] in {"COMPLETED", "FAILED", "PARTIAL"}:
                break
        assert output["status"] == "COMPLETED", output
        assert output["verification"]["supported"] is True
        with Session(create_engine(database_url)) as session:
            prompts = session.scalars(select(PromptVersionRecord)).all()
            assert {(item.component, item.version) for item in prompts} == {
                ("router", "v1"), ("planner", "v2"), ("diagnosis", "v2"),
            }
