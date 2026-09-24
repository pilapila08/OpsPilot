"""Allowlisted Prometheus range queries with bounded numeric output."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes.models import ContainerName, NamespaceName, ResourceName
from opspilot.integrations.prometheus import PrometheusBoundaryError, PrometheusReader
from opspilot.tools.models import RetryPolicy, ToolDefinition, ToolRiskLevel
from opspilot.tools.registry import ToolRegistry

MetricKind = Literal["cpu", "memory", "latency", "error_rate", "http_503_rate"]
MetricUnit = Literal["cores", "bytes", "seconds", "ratio"]
_UNITS: dict[MetricKind, MetricUnit] = {
    "cpu": "cores", "memory": "bytes", "latency": "seconds",
    "error_rate": "ratio", "http_503_rate": "ratio",
}


class MetricInputV2(StrictSchema):
    namespace: NamespaceName
    workload_name: ResourceName
    pod_name: ResourceName | None = None
    container_name: ContainerName | None = None
    window_seconds: int = Field(default=300, ge=120, le=3_600)
    step_seconds: int = Field(default=30, ge=15, le=300)
    max_samples: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def bound_sample_count(self) -> MetricInputV2:
        if (self.pod_name is None) != (self.container_name is None):
            raise ValueError("exact Pod metric scope requires pod and container together")
        if self.window_seconds // self.step_seconds + 1 > self.max_samples:
            raise ValueError("Prometheus window/step exceeds sample budget")
        return self


class Service503InputV2(StrictSchema):
    namespace: NamespaceName
    service_name: ResourceName
    window_seconds: int = Field(default=300, ge=120, le=3_600)
    step_seconds: int = Field(default=30, ge=15, le=300)
    max_samples: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def bound_sample_count(self) -> Service503InputV2:
        if self.window_seconds // self.step_seconds + 1 > self.max_samples:
            raise ValueError("Prometheus window/step exceeds sample budget")
        return self


class MetricSampleV2(StrictSchema):
    sampled_at: datetime
    value: float = Field(ge=0, allow_inf_nan=False)


class MetricOutputV2(StrictSchema):
    namespace: NamespaceName
    workload_name: ResourceName
    pod_name: ResourceName | None
    container_name: ContainerName | None
    metric: MetricKind
    unit: MetricUnit
    status: Literal["present", "missing", "stale"]
    observed_at: datetime
    samples: tuple[MetricSampleV2, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_status(self) -> MetricOutputV2:
        if self.unit != _UNITS[self.metric]:
            raise ValueError("metric unit differs from fixed template")
        if (self.status == "present") != bool(self.samples):
            raise ValueError("present requires samples; missing/stale cannot fabricate them")
        return self


def _query(kind: MetricKind, arguments: MetricInputV2) -> str:
    if kind == "http_503_rate":
        scope = f'namespace="{arguments.namespace}",service="{arguments.workload_name}"'
        return (
            f'sum(rate(http_requests_total{{{scope},status="503"}}[5m])) / '
            f'clamp_min(sum(rate(http_requests_total{{{scope}}}[5m])),1e-9)'
        )
    pod = (
        f'pod="{arguments.pod_name}"'
        if arguments.pod_name is not None
        else f'pod=~"^{re.escape(arguments.workload_name)}(-.*)?$"'
    )
    scope = f'namespace="{arguments.namespace}",{pod}'
    if arguments.container_name is not None:
        scope += f',container="{arguments.container_name}"'
    if kind == "cpu":
        return f'sum(rate(container_cpu_usage_seconds_total{{{scope},container!="POD"}}[2m]))'
    if kind == "memory":
        return f'sum(container_memory_working_set_bytes{{{scope},container!="POD"}})'
    if kind == "latency":
        return f'histogram_quantile(0.95,sum(rate(http_request_duration_seconds_bucket{{{scope}}}[5m])) by (le))'
    return (
        f'sum(rate(http_requests_total{{{scope},status=~"5.."}}[5m])) / '
        f'clamp_min(sum(rate(http_requests_total{{{scope}}}[5m])),1e-9)'
    )


class PrometheusToolHandlersV2:
    def __init__(
        self, reader: PrometheusReader,
        *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._clock = clock or (lambda: datetime.now(UTC))

    async def query(self, kind: MetricKind, arguments: MetricInputV2) -> MetricOutputV2:
        now = self._clock().astimezone(UTC)
        raw = await self._reader.query_range(
            query=_query(kind, arguments),
            start=now - timedelta(seconds=arguments.window_seconds),
            end=now, step_seconds=arguments.step_seconds,
        )
        if raw.get("status") != "success":
            raise PrometheusBoundaryError("Prometheus query returned an invalid status")
        data = raw.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "matrix":
            raise PrometheusBoundaryError("Prometheus range result has invalid shape")
        result = data.get("result")
        if not isinstance(result, list) or len(result) > 1:
            raise PrometheusBoundaryError("Prometheus query returned unexpected series")
        if not result:
            return MetricOutputV2(
                namespace=arguments.namespace, workload_name=arguments.workload_name,
                pod_name=arguments.pod_name, container_name=arguments.container_name,
                metric=kind, unit=_UNITS[kind], status="missing", observed_at=now,
                samples=(),
            )
        series = result[0]
        if not isinstance(series, dict) or not isinstance(series.get("metric"), dict):
            raise PrometheusBoundaryError("Prometheus series has invalid shape")
        values = series.get("values")
        if not isinstance(values, list) or len(values) > arguments.max_samples:
            raise PrometheusBoundaryError("Prometheus series exceeds sample limit")
        samples: list[MetricSampleV2] = []
        earliest = now - timedelta(seconds=arguments.window_seconds + arguments.step_seconds)
        for raw_sample in values:
            if not isinstance(raw_sample, list) or len(raw_sample) != 2:
                raise PrometheusBoundaryError("Prometheus sample has invalid shape")
            stamp, raw_value = raw_sample
            if type(stamp) not in (int, float) or not isinstance(raw_value, str):
                raise PrometheusBoundaryError("Prometheus sample has invalid types")
            try:
                value = float(raw_value)
                sampled_at = datetime.fromtimestamp(cast(float, stamp), UTC)
            except (ValueError, OverflowError, OSError):
                raise PrometheusBoundaryError("Prometheus sample is invalid") from None
            if not math.isfinite(value) or value < 0 or sampled_at < earliest or sampled_at > now + timedelta(seconds=30):
                raise PrometheusBoundaryError("Prometheus sample is invalid or outside window")
            if kind in {"error_rate", "http_503_rate"} and value > 1:
                raise PrometheusBoundaryError("Prometheus ratio exceeds one")
            if samples and sampled_at <= samples[-1].sampled_at:
                raise PrometheusBoundaryError("Prometheus samples are not increasing")
            samples.append(MetricSampleV2(sampled_at=sampled_at, value=value))
        if not samples:
            status: Literal["present", "missing", "stale"] = "missing"
        elif now - samples[-1].sampled_at > timedelta(seconds=max(60, 2 * arguments.step_seconds)):
            status = "stale"
            samples.clear()
        else:
            status = "present"
        return MetricOutputV2(
            namespace=arguments.namespace, workload_name=arguments.workload_name,
            pod_name=arguments.pod_name, container_name=arguments.container_name,
            metric=kind, unit=_UNITS[kind], status=status,
            observed_at=now, samples=tuple(samples),
        )

    async def query_service_503(self, arguments: Service503InputV2) -> MetricOutputV2:
        return await self.query("http_503_rate", MetricInputV2(
            namespace=arguments.namespace, workload_name=arguments.service_name,
            window_seconds=arguments.window_seconds,
            step_seconds=arguments.step_seconds, max_samples=arguments.max_samples,
        ))


def prometheus_tool_definitions_v2(
    reader: PrometheusReader,
) -> tuple[ToolDefinition[Any, Any], ...]:
    handlers = PrometheusToolHandlersV2(reader)
    retry = RetryPolicy(
        max_retries=1,
        retryable_errors=(ErrorCode.TOOL_TIMEOUT, ErrorCode.EXTERNAL_SERVICE_ERROR),
    )

    def make_handler(kind: MetricKind) -> Callable[[MetricInputV2], Any]:
        async def handle(arguments: MetricInputV2) -> MetricOutputV2:
            return await handlers.query(kind, arguments)

        return handle

    kinds: tuple[MetricKind, ...] = ("cpu", "memory", "latency", "error_rate")
    definitions = tuple(
        ToolDefinition(
            name=f"prometheus.query_{kind}", description=f"Read bounded {kind} range for one workload.",
            risk_level=ToolRiskLevel.READ_ONLY,
            input_model=MetricInputV2, output_model=MetricOutputV2,
            handler=make_handler(kind), source="prometheus", version="v2",
            timeout_seconds=12, retry_policy=retry,
        )
        for kind in kinds
    )
    return (*definitions, ToolDefinition(
        name="prometheus.query_http_503_rate",
        description="Read bounded HTTP 503 ratio for one scoped Service.",
        risk_level=ToolRiskLevel.READ_ONLY,
        input_model=Service503InputV2, output_model=MetricOutputV2,
        handler=handlers.query_service_503, source="prometheus", version="v2",
        timeout_seconds=12, retry_policy=retry,
    ))


def register_prometheus_tools_v2(registry: ToolRegistry, reader: PrometheusReader) -> None:
    for definition in prometheus_tool_definitions_v2(reader):
        registry.register(definition)
