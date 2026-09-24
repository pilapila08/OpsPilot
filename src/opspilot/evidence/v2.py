"""V2-only OOM and metric Evidence, leaving the V1 extractor frozen."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from opspilot.evidence.extractors import EvidenceExtractionError, EvidenceExtractorRegistry
from opspilot.evidence.models import Evidence, EvidenceAttribute
from opspilot.tools.kubernetes.models import (
    DeploymentOutput, PodEventsOutput, PodStatusOutput,
)
from opspilot.tools.kubernetes.v2_models import ServiceMembershipOutputV2
from opspilot.tools.models import ToolInvocation, ToolResponse
from opspilot.tools.prometheus import MetricOutputV2

_MEMORY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$")
_SCALE = {
    None: 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
}


class V2EvidenceExtractorRegistry(EvidenceExtractorRegistry):
    def extract(
        self, *, invocation: ToolInvocation, response: ToolResponse,
        trace_id: str, tool_attempt_id: str, collected_at: datetime,
        evidence_id_factory: Callable[[], str],
    ) -> tuple[Evidence, ...]:
        if invocation.tool.startswith("prometheus.query_"):
            return _metric_evidence(
                invocation, response, trace_id, tool_attempt_id,
                collected_at, evidence_id_factory,
            )
        if invocation.tool == "k8s.get_service_membership":
            return _membership_evidence(
                invocation, response, trace_id, tool_attempt_id,
                collected_at, evidence_id_factory,
            )
        base = super().extract(
            invocation=invocation, response=response, trace_id=trace_id,
            tool_attempt_id=tool_attempt_id, collected_at=collected_at,
            evidence_id_factory=evidence_id_factory,
        )
        assert response.data is not None
        extra: list[Evidence] = []
        payload = json.dumps(response.data)
        if invocation.tool == "k8s.get_pod_status":
            status = PodStatusOutput.model_validate_json(payload, strict=True)
            for condition in status.conditions:
                if condition.type != "Ready" or condition.status not in {"True", "False"}:
                    continue
                observed = (
                    _time(condition.last_transition_time, collected_at)
                    if condition.last_transition_time is not None else None
                ) or collected_at
                extra.append(Evidence(
                    evidence_id=evidence_id_factory(), trace_id=trace_id,
                    tool_call_id=tool_attempt_id, source="kubernetes_readiness",
                    resource=f"{status.namespace}/{status.pod_name}",
                    observed_at=observed, collected_at=collected_at,
                    content="Pod Ready condition was observed.",
                    source_confidence=1.0, raw_result_ref=tool_attempt_id,
                    attributes=(EvidenceAttribute(
                        key="pod_ready", value=condition.status == "True",
                    ),),
                ))
            for container in status.containers:
                if container.last_reason != "OOMKilled" or container.last_finished_at is None:
                    continue
                finished = _time(container.last_finished_at, collected_at)
                if finished is None:
                    continue
                extra.append(Evidence(
                    evidence_id=evidence_id_factory(), trace_id=trace_id,
                    tool_call_id=tool_attempt_id, source="kubernetes_oom_termination",
                    resource=f"{status.namespace}/{status.pod_name}:{container.name}",
                    observed_at=finished, collected_at=collected_at,
                    content="Container last terminated with OOMKilled.",
                    source_confidence=1.0, raw_result_ref=tool_attempt_id,
                    attributes=(
                        EvidenceAttribute(key="termination_reason", value="OOMKilled"),
                        EvidenceAttribute(key="restart_count", value=container.restart_count),
                    ),
                ))
        elif invocation.tool == "k8s.get_pod_events":
            events = PodEventsOutput.model_validate_json(payload, strict=True)
            for event in events.events:
                if event.reason != "Unhealthy" or "readiness probe failed" not in event.message.lower():
                    continue
                observed = (
                    _time(event.last_timestamp, collected_at)
                    if event.last_timestamp is not None else None
                ) or collected_at
                extra.append(Evidence(
                    evidence_id=evidence_id_factory(), trace_id=trace_id,
                    tool_call_id=tool_attempt_id, source="kubernetes_readiness_event",
                    resource=f"{events.namespace}/{events.pod_name}",
                    observed_at=observed, collected_at=collected_at,
                    content="Kubernetes reported a readiness probe failure.",
                    source_confidence=1.0, raw_result_ref=tool_attempt_id,
                    attributes=(
                        EvidenceAttribute(key="readiness_failure", value=True),
                        EvidenceAttribute(key="failure_count", value=event.count),
                    ),
                ))
        elif invocation.tool == "k8s.get_deployment":
            deployment = DeploymentOutput.model_validate_json(payload, strict=True)
            for deployed_container in deployment.containers:
                probe = deployed_container.readiness_probe
                if probe is not None:
                    attributes = [
                        EvidenceAttribute(key="probe_kind", value=probe.kind),
                        EvidenceAttribute(key="probe_port", value=probe.port),
                    ]
                    extra.append(Evidence(
                        evidence_id=evidence_id_factory(), trace_id=trace_id,
                        tool_call_id=tool_attempt_id, source="kubernetes_readiness_probe",
                        resource=f"{deployment.namespace}/deployment/{deployment.name}:{deployed_container.name}",
                        observed_at=collected_at, collected_at=collected_at,
                        content="Deployment declares a readiness probe.",
                        source_confidence=1.0, raw_result_ref=tool_attempt_id,
                        attributes=tuple(attributes),
                    ))
                quantity = deployed_container.resources.limits.get("memory")
                amount = _memory_bytes(quantity) if quantity is not None else None
                if amount is None:
                    continue
                extra.append(Evidence(
                    evidence_id=evidence_id_factory(), trace_id=trace_id,
                    tool_call_id=tool_attempt_id, source="kubernetes_memory_limit",
                    resource=f"{deployment.namespace}/deployment/{deployment.name}:{deployed_container.name}",
                    observed_at=collected_at, collected_at=collected_at,
                    content="Deployment declares a container memory limit.",
                    source_confidence=1.0, raw_result_ref=tool_attempt_id,
                    attributes=(EvidenceAttribute(key="memory_limit_bytes", value=amount),),
                ))
        return (*base, *extra)


def _membership_evidence(
    invocation: ToolInvocation, response: ToolResponse, trace_id: str,
    tool_attempt_id: str, collected_at: datetime,
    evidence_id_factory: Callable[[], str],
) -> tuple[Evidence, ...]:
    if not response.success or response.data is None:
        raise EvidenceExtractionError("Membership Evidence requires a successful Tool response")
    if (
        response.metadata.call_id != invocation.call_id
        or response.metadata.tool_name != invocation.tool
    ):
        raise EvidenceExtractionError("Membership response differs from invocation")
    try:
        output = ServiceMembershipOutputV2.model_validate_json(
            json.dumps(response.data), strict=True,
        )
    except ValidationError:
        raise EvidenceExtractionError("Membership output failed schema validation") from None
    if (
        output.namespace != invocation.arguments.get("namespace")
        or output.service_name != invocation.arguments.get("service_name")
        or output.deployment_name != invocation.arguments.get("deployment_name")
    ):
        raise EvidenceExtractionError("Membership identity differs from invocation")
    active_ready = [pod for pod in output.pods if pod.ready is True and not pod.terminating]
    matching_ready = [pod for pod in active_ready if pod.selector_matches]
    return (Evidence(
        evidence_id=evidence_id_factory(), trace_id=trace_id,
        tool_call_id=tool_attempt_id, source="kubernetes_service_membership",
        resource=f"{output.namespace}/service/{output.service_name}",
        observed_at=min(output.observed_at, collected_at), collected_at=collected_at,
        content="Bounded Deployment Pod labels were compared with the Service selector.",
        source_confidence=1.0, raw_result_ref=tool_attempt_id,
        attributes=(
            EvidenceAttribute(key="service_uid", value=output.service_uid),
            EvidenceAttribute(key="service_resource_version", value=output.service_resource_version),
            EvidenceAttribute(key="deployment_uid", value=output.deployment_uid),
            EvidenceAttribute(key="deployment_generation", value=output.deployment_generation),
            EvidenceAttribute(key="observed_generation", value=output.observed_generation),
            EvidenceAttribute(key="selector_count", value=output.selector_count),
            EvidenceAttribute(key="active_ready_pod_count", value=len(active_ready)),
            EvidenceAttribute(key="matching_ready_pod_count", value=len(matching_ready)),
            EvidenceAttribute(key="membership_truncated", value=output.truncated),
            EvidenceAttribute(key="rollout_ambiguous", value=output.rollout_ambiguous),
        ),
    ),)


def _metric_evidence(
    invocation: ToolInvocation, response: ToolResponse, trace_id: str,
    tool_attempt_id: str, collected_at: datetime,
    evidence_id_factory: Callable[[], str],
) -> tuple[Evidence, ...]:
    if not response.success or response.data is None:
        raise EvidenceExtractionError("Metrics Evidence requires a successful Tool response")
    if (
        response.metadata.call_id != invocation.call_id
        or response.metadata.tool_name != invocation.tool
        or response.data.get("namespace") != invocation.arguments.get("namespace")
    ):
        raise EvidenceExtractionError("Metrics response differs from invocation")
    try:
        output = MetricOutputV2.model_validate_json(json.dumps(response.data), strict=True)
    except ValidationError:
        raise EvidenceExtractionError("Metrics output failed schema validation") from None
    target_key = "service_name" if output.metric == "http_503_rate" else "workload_name"
    if (
        invocation.tool != f"prometheus.query_{output.metric}"
        or output.workload_name != invocation.arguments.get(target_key)
        or output.pod_name != invocation.arguments.get("pod_name")
        or output.container_name != invocation.arguments.get("container_name")
    ):
        raise EvidenceExtractionError("Metrics identity differs from invocation")
    resource = (
        f"{output.namespace}/service/{output.workload_name}"
        if output.metric == "http_503_rate" else
        f"{output.namespace}/{output.pod_name}:{output.container_name}"
        if output.pod_name is not None and output.container_name is not None
        else f"{output.namespace}/deployment/{output.workload_name}"
    )
    if output.status != "present":
        return (Evidence(
            evidence_id=evidence_id_factory(), trace_id=trace_id,
            tool_call_id=tool_attempt_id, source=f"prometheus_{output.metric}",
            resource=resource, observed_at=min(output.observed_at, collected_at),
            collected_at=collected_at, content="Prometheus has no fresh metric samples.",
            source_confidence=1.0, raw_result_ref=tool_attempt_id,
            attributes=(EvidenceAttribute(key="sample_status", value=output.status),),
        ),)
    return tuple(Evidence(
        evidence_id=evidence_id_factory(), trace_id=trace_id,
        tool_call_id=tool_attempt_id, source=f"prometheus_{output.metric}",
        resource=resource, observed_at=min(sample.sampled_at, collected_at),
        collected_at=collected_at, content="Prometheus reported a bounded metric sample.",
        source_confidence=1.0, raw_result_ref=tool_attempt_id,
        attributes=(
            EvidenceAttribute(key="metric_value", value=sample.value),
            EvidenceAttribute(key="metric_unit", value=output.unit),
        ),
    ) for sample in output.samples)


def _time(value: str, collected_at: datetime) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    normalized = parsed.astimezone(UTC)
    return normalized if normalized <= collected_at else None


def _memory_bytes(value: str) -> int | None:
    match = _MEMORY.fullmatch(value)
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1)) * _SCALE[match.group(2)]
    except InvalidOperation:
        return None
    if amount <= 0 or amount != amount.to_integral_value() or amount > 2**63 - 1:
        return None
    return int(amount)
