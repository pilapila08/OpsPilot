"""Conservative Service 503 assessment from scoped, time-aligned observations."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import cast

from opspilot.diagnosis.v2 import V2Assessment
from opspilot.evidence.models import Claim, Contradiction, Evidence, MissingEvidence, Verification
from opspilot.routing.v2 import IntentV2


def _attr(item: Evidence, key: str) -> object:
    return next((entry.value for entry in item.attributes if entry.key == key), None)


def _near(left: Evidence, right: Evidence, seconds: int = 90) -> bool:
    return abs(left.observed_at - right.observed_at) <= timedelta(seconds=seconds)


def _positive_ratio(item: Evidence) -> bool:
    value = _attr(item, "metric_value")
    return type(value) in (int, float) and 0 < float(cast(float, value)) <= 1


def _positive_int(item: Evidence, key: str) -> bool:
    value = _attr(item, key)
    return type(value) is int and value > 0


def _port_discrepancy(service: Evidence, endpoint: Evidence) -> bool:
    target = _attr(service, "target_port")
    encoded = _attr(endpoint, "endpoint_ports")
    if type(target) is not int or not isinstance(encoded, str):
        return False
    try:
        ports = json.loads(encoded)
    except (ValueError, TypeError):
        return False
    if not isinstance(ports, list) or len(ports) != 1:
        return False
    port = ports[0]
    return (
        isinstance(port, list) and len(port) == 3
        and port[0] == _attr(service, "service_port_name")
        and port[1] == "TCP" and type(port[2]) is int
        and 1 <= port[2] <= 65_535 and port[2] != target
    )


class Service503VerifierV2:
    def verify(
        self, *, intent: IntentV2, evidence: tuple[Evidence, ...],
        force_partial: bool,
    ) -> V2Assessment:
        if not evidence or len(evidence) > 100:
            raise ValueError("Service 503 verifier requires bounded Evidence")
        if len({item.trace_id for item in evidence}) != 1 or len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("Service 503 verifier requires unique current-Trace Evidence")
        namespace = intent.target.namespace
        relevant = tuple(item for item in evidence if item.resource.startswith(f"{namespace}/"))
        if not relevant:
            raise ValueError("Service 503 verifier requires namespace-scoped Evidence")
        missing: list[MissingEvidence] = []
        contradictions: list[Contradiction] = []
        if intent.fault_family != "service_503" or intent.target.kind not in {"service", "ingress"}:
            missing.append(MissingEvidence(
                requirement="service_503_target", reason="A Service or Ingress 503 target is required.",
            ))

        routes = [item for item in relevant if item.source == "kubernetes_ingress"
                  and (intent.target.kind != "ingress" or item.resource == f"{namespace}/ingress/{intent.target.resource}")
                  and (intent.target.kind != "service" or _attr(item, "backend_service") == intent.target.resource)]
        backends = {(_attr(item, "backend_service"), _attr(item, "backend_port")) for item in routes}
        if len(backends) != 1:
            missing.append(MissingEvidence(
                requirement="ingress_route", reason="A unique scoped Ingress Service/port backend is unavailable.",
            ))
        service_name, backend_port = next(iter(backends)) if len(backends) == 1 else (None, None)
        if intent.target.kind == "service" and service_name != intent.target.resource:
            service_name = None
        resource = f"{namespace}/service/{service_name}" if isinstance(service_name, str) else ""

        services = [item for item in relevant if item.source == "kubernetes_service"
                    and item.resource == resource and
                    (_attr(item, "service_port") == backend_port or _attr(item, "service_port_name") == backend_port)]
        if len(services) != 1:
            missing.append(MissingEvidence(
                requirement="service_port", reason="The Ingress backend has no unique matching Service port.",
            ))
        service = services[0] if len(services) == 1 else None
        if service is not None and _attr(service, "service_type") == "ExternalName":
            contradictions.append(Contradiction(
                evidence_ids=(service.evidence_id,), reason="ExternalName has no Kubernetes EndpointSlice backend.",
            ))

        snapshots = [item for item in relevant if item.source == "kubernetes_endpoints"
                     and item.resource == resource]
        if len(snapshots) != 1:
            missing.append(MissingEvidence(
                requirement="endpoint_snapshot", reason="One unambiguous EndpointSlice snapshot is required.",
            ))
        endpoint = snapshots[0] if len(snapshots) == 1 else None
        if endpoint is not None:
            if _attr(endpoint, "endpoint_snapshot_truncated") is not False:
                missing.append(MissingEvidence(
                    requirement="complete_endpoints", reason="EndpointSlice snapshot is incomplete.",
                ))
            if _attr(endpoint, "ready_endpoint_count") is not None and _attr(endpoint, "ready_endpoint_count") != 0:
                contradictions.append(Contradiction(
                    evidence_ids=(endpoint.evidence_id,),
                    reason="Ready endpoints exist; no-ready-backend cause is contradicted.",
                ))
            if type(_attr(endpoint, "ready_endpoint_count")) is not int:
                missing.append(MissingEvidence(
                    requirement="ready_endpoint_count", reason="Ready endpoint count is unavailable.",
                ))
            if _attr(endpoint, "unknown_ready_endpoint_count") != 0:
                missing.append(MissingEvidence(
                    requirement="explicit_readiness", reason="Endpoint readiness is unknown or not recorded.",
                ))
            if _attr(endpoint, "terminating_serving_endpoint_count") != 0:
                missing.append(MissingEvidence(
                    requirement="rollout_stability", reason="Terminating backends may still be serving during rollout.",
                ))
        if service is not None and endpoint is not None and not _near(service, endpoint, 60):
            missing.append(MissingEvidence(
                requirement="topology_window", reason="Service and EndpointSlice snapshots are not contemporaneous.",
            ))

        metrics = [item for item in relevant if item.source == "prometheus_http_503_rate"
                   and item.resource == resource and _attr(item, "metric_unit") == "ratio"
                   and _positive_ratio(item)
                   and endpoint is not None and _near(item, endpoint)]
        metric = max(metrics, key=lambda item: item.observed_at) if metrics else None
        if metric is None:
            missing.append(MissingEvidence(
                requirement="fresh_503_metric", reason="No positive Service-scoped 503 sample overlaps the topology snapshot.",
            ))
        if service is not None and endpoint is not None and metric is not None and routes:
            if not any(_near(route, endpoint, 60) for route in routes):
                missing.append(MissingEvidence(
                    requirement="route_window", reason="Ingress route is not contemporaneous with endpoints.",
                ))
        memberships = [item for item in relevant
                       if item.source == "kubernetes_service_membership" and item.resource == resource]
        if len(memberships) > 1:
            missing.append(MissingEvidence(
                requirement="membership_snapshot", reason="Multiple Pod membership snapshots are ambiguous.",
            ))
        membership = memberships[0] if len(memberships) == 1 else None
        selector_mismatch = False
        if membership is not None:
            if (
                service is None or endpoint is None
                or _attr(membership, "service_uid") != _attr(service, "service_uid")
                or _attr(membership, "service_resource_version") != _attr(service, "service_resource_version")
                or not _near(membership, endpoint, 60)
            ):
                missing.append(MissingEvidence(
                    requirement="membership_identity", reason="Pod membership does not match the fresh Service snapshot.",
                ))
            if _attr(membership, "membership_truncated") is not False or _attr(membership, "rollout_ambiguous") is not False:
                missing.append(MissingEvidence(
                    requirement="stable_membership", reason="Pod membership is truncated or rolling out.",
                ))
            selector_mismatch = (
                _positive_int(membership, "selector_count")
                and _positive_int(membership, "active_ready_pod_count")
                and _attr(membership, "matching_ready_pod_count") == 0
            )
        if force_partial:
            missing.append(MissingEvidence(
                requirement="execution_complete", reason="Observation was stopped before full verification.",
            ))
        route = next((item for item in routes if endpoint is not None and _near(item, endpoint, 60)), None)
        cited = tuple(dict.fromkeys(item.evidence_id for item in (route, service, endpoint, metric, membership) if item is not None))
        if not cited:
            cited = (relevant[0].evidence_id,)
        supported = not missing and not contradictions and all(
            item is not None for item in (route, service, endpoint, metric)
        )
        port_anomaly = (
            not missing and route is not None and service is not None
            and endpoint is not None and metric is not None and membership is not None
            and _positive_int(endpoint, "ready_endpoint_count")
            and _positive_int(membership, "active_ready_pod_count")
            and _positive_int(membership, "matching_ready_pod_count")
            and _port_discrepancy(service, endpoint)
            and all(item.reason.startswith("Ready endpoints exist") for item in contradictions)
        )
        if port_anomaly:
            contradictions.clear()
            missing.append(MissingEvidence(
                requirement="port_causality",
                reason="Gateway-side 503 provenance or a safe traffic observation is required to attribute 503s to the port discrepancy.",
            ))
        if not supported and not missing and not contradictions:
            missing.append(MissingEvidence(
                requirement="independent_signals", reason="Four independent scoped observations are required.",
            ))
        claim = Claim(
            claim_id=("claim_service_503_port_anomaly" if port_anomaly else
                      "claim_service_503_selector" if supported and selector_mismatch else "claim_service_503_no_ready"),
            text=("Service targetPort differs from the published ready EndpointSlice port during observed 503s; causality is unproven."
                  if port_anomaly else
                  "The Service selector excludes all observed ready Deployment Pods during 503s."
                  if supported and selector_mismatch else
                  "503s coincide with no ready EndpointSlice backends for the routed Service."
                  if supported else "The available observations do not establish a no-ready-backend 503 cause."),
            evidence_ids=cited, inference_confidence=(0.9 if selector_mismatch else 0.8) if supported else 0.4 if port_anomaly else 0,
        )
        verification = Verification(
            claim_id=claim.claim_id, supported=supported,
            verification_confidence=(0.9 if selector_mismatch else 0.8) if supported else 0.4 if port_anomaly else 0,
            checked_evidence_ids=tuple(item.evidence_id for item in relevant),
            missing_evidence=tuple(missing), contradictions=tuple(contradictions),
            rationale=("Service, EndpointSlice and stable Pod membership show a port discrepancy, but 503 origin is not proven."
                       if port_anomaly else
                       "A stable Pod label comparison corroborates selector drift with 503 and zero-ready endpoints."
                       if supported and selector_mismatch else
                       "Service 503 metric, Ingress route, Service port and complete zero-ready EndpointSlice snapshot overlap."
                       if supported else "The scoped no-ready-backend hypothesis is incomplete or contradicted."),
        )
        return V2Assessment(
            status="COMPLETED" if supported else "PARTIAL",
            root_cause=("A Service/EndpointSlice port discrepancy was observed; the 503 cause remains unproven."
                        if port_anomaly else
                        f"Service {service_name} selector excluded all observed ready Deployment Pods during 503s."
                        if supported and selector_mismatch else
                        f"Service {service_name} had no ready EndpointSlice backends during observed 503s."
                        if supported else "The Service 503 source remains unproven."),
            recommendation=("Check the Service targetPort and EndpointSlice controller; obtain gateway-side 503 provenance before attributing cause."
                            if port_anomaly else
                            "Inspect backend Pod readiness and Service selector before changing traffic configuration."
                            if supported else "Collect a fresh, complete route/Service/EndpointSlice snapshot and Service-scoped 503 samples."),
            confidence=Decimal("0.9" if selector_mismatch else "0.8") if supported else Decimal("0.4") if port_anomaly else Decimal(0),
            claim=claim, verification=verification,
        )
