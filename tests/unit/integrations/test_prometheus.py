import asyncio
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes.models import JsonObject
from opspilot.integrations.prometheus import (
    PrometheusBoundaryError, PrometheusHttpReader, PrometheusPermissionError,
    PrometheusReader, PrometheusSettings, PrometheusTimeoutError,
    PrometheusUnavailableError,
)
from opspilot.tools import ToolInvocation, ToolRegistry, ToolRiskLevel
from opspilot.tools.prometheus import (
    MetricInputV2, PrometheusToolHandlersV2,
    register_prometheus_tools_v2,
)

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


class FakeReader:
    def __init__(self, values: list[list[JsonValue]] | None = None) -> None:
        self.values = values
        self.queries: list[str] = []

    async def query_range(
        self, *, query: str, start: datetime, end: datetime, step_seconds: int,
    ) -> JsonObject:
        assert step_seconds == 30
        assert (end - start).total_seconds() == 300
        self.queries.append(query)
        return cast(JsonObject, {
            "status": "success",
            "data": {"resultType": "matrix", "result": (
                [] if self.values is None else [{"metric": {}, "values": self.values}]
            )},
        })


def _reader(fake: FakeReader) -> PrometheusReader:
    return cast(PrometheusReader, fake)


def test_four_fixed_metric_tools_are_risk_zero_and_schema_bounded() -> None:
    fake = FakeReader([[datetime.now(UTC).timestamp(), "134217728"]])
    registry = ToolRegistry()
    register_prometheus_tools_v2(registry, _reader(fake))
    assert len(registry.descriptors()) == 4
    assert all(item.risk_level is ToolRiskLevel.READ_ONLY for item in registry.descriptors())
    for kind in ("cpu", "memory", "latency", "error_rate"):
        response = asyncio.run(registry.invoke(ToolInvocation(
            call_id=f"call_{kind}", tool=f"prometheus.query_{kind}",
            arguments={"namespace": "team-a", "workload_name": "api"},
        )))
        assert response.success, response.error
        assert response.data is not None
        assert response.data["metric"] == kind
        assert response.data["status"] == "present"
    assert len(set(fake.queries)) == 4
    assert all('namespace="team-a"' in query for query in fake.queries)
    assert all('pod=~"^api(-.*)?$"' in query for query in fake.queries)
    assert "1e-9" in fake.queries[-1]
    rejected = asyncio.run(registry.invoke(ToolInvocation(
        call_id="call_injected", tool="prometheus.query_memory",
        arguments={"namespace": "team-a", "workload_name": "api", "query": "up"},
    )))
    assert rejected.success is False
    assert rejected.error is not None and rejected.error.code is ErrorCode.INVALID_ARGUMENT
    assert len(fake.queries) == 4


def test_exact_scope_missing_stale_nan_and_window_bounds() -> None:
    arguments = MetricInputV2(
        namespace="team-a", workload_name="api", pod_name="api-001",
        container_name="app",
    )
    missing = FakeReader()
    output = asyncio.run(PrometheusToolHandlersV2(_reader(missing), clock=lambda: NOW).query(
        "memory", arguments,
    ))
    assert output.status == "missing" and output.samples == ()
    assert 'pod="api-001",container="app"' in missing.queries[0]
    stale = FakeReader([[NOW.timestamp() - 180, "100"]])
    output = asyncio.run(PrometheusToolHandlersV2(_reader(stale), clock=lambda: NOW).query(
        "memory", arguments,
    ))
    assert output.status == "stale" and output.samples == ()
    bad = FakeReader([[NOW.timestamp(), "NaN"]])
    with pytest.raises(PrometheusBoundaryError):
        asyncio.run(PrometheusToolHandlersV2(_reader(bad), clock=lambda: NOW).query(
            "memory", arguments,
        ))
    with pytest.raises(ValidationError):
        MetricInputV2(namespace="team-a", workload_name="api", pod_name="api-001")
    with pytest.raises(ValidationError):
        MetricInputV2(namespace="team-a", workload_name="api", window_seconds=3600, step_seconds=15)


def test_https_reader_classifies_http_and_invalid_payload_without_leaking_token() -> None:
    settings = PrometheusSettings.model_validate({
        "base_url": "https://metrics.example.test", "bearer_token": SecretStr("do-not-leak"),
    })
    assert "do-not-leak" not in repr(settings)
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403, text="token=do-not-leak")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    reader = PrometheusHttpReader(settings, client=client)
    with pytest.raises(PrometheusPermissionError) as caught:
        asyncio.run(reader.query_range(query="up", start=NOW, end=NOW, step_seconds=30))
    assert "do-not-leak" not in str(caught.value)
    assert seen[0].url.path == "/api/v1/query_range"
    assert seen[0].headers["authorization"] == "Bearer do-not-leak"
    asyncio.run(client.aclose())

    for status, error_type in (
        (503, PrometheusUnavailableError), (200, PrometheusBoundaryError),
    ):
        another = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="not-json")
        ))
        with pytest.raises(error_type):
            asyncio.run(PrometheusHttpReader(settings, client=another).query_range(
                query="up", start=NOW, end=NOW, step_seconds=30,
            ))
        asyncio.run(another.aclose())

    with pytest.raises(ValidationError):
        PrometheusSettings.model_validate({"base_url": "http://metrics.example.test"})
    with pytest.raises(ValidationError):
        PrometheusSettings.model_validate({"base_url": "https://metrics.example.test/foreign"})


def test_https_reader_timeout_is_classified() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret upstream detail")

    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    reader = PrometheusHttpReader(PrometheusSettings.model_validate({
        "base_url": "https://metrics.example.test",
    }), client=client)
    with pytest.raises(PrometheusTimeoutError) as caught:
        asyncio.run(reader.query_range(query="up", start=NOW, end=NOW, step_seconds=30))
    assert "secret upstream detail" not in str(caught.value)
    asyncio.run(client.aclose())
