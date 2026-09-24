from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue

from opspilot.diagnosis.service_503_v2 import Service503VerifierV2
from opspilot.evidence.models import Evidence
from opspilot.evidence.v2 import V2EvidenceExtractorRegistry
from opspilot.routing.v2 import IntentV2, V2Target
from opspilot.tools import ToolInvocation
from opspilot.tools.models import ToolMetadata, ToolResponse

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
NS = "team-a"


def _extract(
    tool: str, target_key: str, target: str, data: dict[str, object],
    number: int,
) -> tuple[Evidence, ...]:
    call_id = f"call_{number}"
    invocation = ToolInvocation(
        call_id=call_id, tool=tool,
        arguments={"namespace": NS, target_key: target},
    )
    response = ToolResponse(
        success=True, data=cast(dict[str, JsonValue], data), error=None,
        metadata=ToolMetadata(
            call_id=call_id, tool_name=tool, source="test", duration_ms=1,
            tool_version="v2",
        ),
    )
    index = iter(range(number * 10, number * 10 + 10))
    return V2EvidenceExtractorRegistry().extract(
        invocation=invocation, response=response, trace_id="trace_503",
        tool_attempt_id=f"tool_{number}", collected_at=NOW,
        evidence_id_factory=lambda: f"ev_{next(index)}",
    )


def test_tool_outputs_to_503_verification_preserve_topology_and_provenance() -> None:
    ingress = _extract("k8s.get_ingress", "ingress_name", "front", {
        "namespace": NS, "name": "front", "uid": "ingress-1", "resource_version": "1",
        "routes": [{"host_sha256": None, "path": "/", "path_type": "Prefix",
                    "service_name": "api", "service_port": 80}],
    }, 1)
    service = _extract("k8s.get_service", "service_name", "api", {
        "namespace": NS, "name": "api", "uid": "service-1", "resource_version": "1",
        "selector": {"app": "api"}, "service_type": "ClusterIP",
        "ports": [{"name": "http", "protocol": "TCP", "port": 80, "target_port": 8080}],
    }, 2)
    endpoints = _extract("k8s.get_endpoints", "service_name", "api", {
        "namespace": NS, "service_name": "api", "truncated": False,
        "observed_at": NOW.isoformat(),
        "slices": [{
            "name": "api-1", "uid": "slice-1", "resource_version": "1",
            "address_type": "IPv4", "ports": [{"name": "http", "protocol": "TCP", "port": 8080}],
            "endpoints": [{"address_count": 1, "ready": False, "serving": False,
                           "terminating": False, "target_kind": "Pod",
                           "target_name": "api-001", "target_uid": "pod-1"}],
        }],
    }, 3)
    metric = _extract("prometheus.query_http_503_rate", "service_name", "api", {
        "namespace": NS, "workload_name": "api", "pod_name": None,
        "container_name": None, "metric": "http_503_rate", "unit": "ratio",
        "status": "present", "observed_at": NOW.isoformat(),
        "samples": [{"sampled_at": NOW.isoformat(), "value": 0.25}],
    }, 4)
    evidence = (*ingress, *service, *endpoints, *metric)
    assert len(evidence) == 4
    assert all(item.raw_result_ref == f"tool_{index}" for index, item in enumerate(evidence, 1))
    endpoint_attrs = {item.key: item.value for item in endpoints[0].attributes}
    assert endpoint_attrs["unknown_ready_endpoint_count"] == 0
    assert endpoint_attrs["endpoint_ports"] == '[["http", "TCP", 8080]]'
    assessment = Service503VerifierV2().verify(
        intent=IntentV2(
            domain="microservice", fault_family="service_503",
            target=V2Target(namespace=NS, kind="service", resource="api"),
        ), evidence=evidence, force_partial=False,
    )
    assert assessment.status == "COMPLETED"
    assert assessment.verification.supported is True
