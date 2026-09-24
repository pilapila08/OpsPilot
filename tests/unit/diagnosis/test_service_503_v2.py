from datetime import UTC, datetime, timedelta

from opspilot.diagnosis.service_503_v2 import Service503VerifierV2
from opspilot.evidence.models import Evidence, EvidenceAttribute, EvidenceScalar
from opspilot.routing.v2 import IntentV2, V2Target

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
INTENT = IntentV2(
    domain="microservice", fault_family="service_503",
    target=V2Target(namespace="team-a", kind="service", resource="api"),
)


def _evidence(
    number: int, source: str, resource: str, attributes: dict[str, EvidenceScalar],
    *, seconds: int = 0,
) -> Evidence:
    return Evidence(
        evidence_id=f"ev_{number}", trace_id="trace_503", tool_call_id=f"tool_{number}",
        source=source, resource=resource,
        observed_at=NOW + timedelta(seconds=seconds),
        collected_at=NOW + timedelta(seconds=max(seconds, 0)),
        content="Normalized observation.", source_confidence=1.0,
        attributes=tuple(EvidenceAttribute(key=key, value=value) for key, value in attributes.items()),
    )


def _observations() -> tuple[Evidence, ...]:
    return (
        _evidence(1, "kubernetes_ingress", "team-a/ingress/front",
                  {"backend_service": "api", "backend_port": 80}),
        _evidence(2, "kubernetes_service", "team-a/service/api",
                  {"service_port": 80, "service_port_name": "http", "target_port": 8080,
                   "service_type": "ClusterIP"}),
        _evidence(3, "kubernetes_endpoints", "team-a/service/api",
                  {"endpoint_count": 2, "ready_endpoint_count": 0,
                   "endpoint_snapshot_truncated": False, "endpoint_slice_count": 1,
                   "unknown_ready_endpoint_count": 0,
                   "terminating_serving_endpoint_count": 0}),
        _evidence(4, "prometheus_http_503_rate", "team-a/service/api",
                  {"metric_value": 0.3, "metric_unit": "ratio"}),
    )


def test_zero_ready_backend_requires_four_scoped_signals() -> None:
    assessment = Service503VerifierV2().verify(
        intent=INTENT, evidence=_observations(), force_partial=False,
    )
    assert assessment.status == "COMPLETED"
    assert assessment.verification.supported
    assert set(assessment.claim.evidence_ids) == {"ev_1", "ev_2", "ev_3", "ev_4"}


def test_ready_endpoint_contradicts_no_ready_cause() -> None:
    items = list(_observations())
    items[2] = _evidence(3, "kubernetes_endpoints", "team-a/service/api",
                         {"endpoint_count": 2, "ready_endpoint_count": 1,
                          "endpoint_snapshot_truncated": False})
    assessment = Service503VerifierV2().verify(
        intent=INTENT, evidence=tuple(items), force_partial=False,
    )
    assert assessment.status == "PARTIAL"
    assert assessment.verification.contradictions[0].evidence_ids == ("ev_3",)


def test_incomplete_or_ambiguous_rollout_snapshot_stays_partial() -> None:
    items = list(_observations())
    items[2] = _evidence(3, "kubernetes_endpoints", "team-a/service/api",
                         {"endpoint_count": 100, "ready_endpoint_count": 0,
                          "endpoint_snapshot_truncated": True})
    for candidate in (tuple(items), (*_observations(), _evidence(
        5, "kubernetes_endpoints", "team-a/service/api",
        {"endpoint_count": 2, "ready_endpoint_count": 0,
         "endpoint_snapshot_truncated": False}, seconds=15,
    ))):
        assessment = Service503VerifierV2().verify(
            intent=INTENT, evidence=candidate, force_partial=False,
        )
        assert assessment.status == "PARTIAL"
        assert assessment.verification.missing_evidence


def test_unknown_readiness_or_terminating_serving_prevents_root_cause() -> None:
    for extra in ({"unknown_ready_endpoint_count": 1},
                  {"terminating_serving_endpoint_count": 1}):
        items = list(_observations())
        attrs = {entry.key: entry.value for entry in items[2].attributes}
        attrs.update(extra)
        items[2] = _evidence(3, "kubernetes_endpoints", "team-a/service/api", attrs)
        assessment = Service503VerifierV2().verify(
            intent=INTENT, evidence=tuple(items), force_partial=False,
        )
        assert assessment.status == "PARTIAL"
        assert assessment.verification.missing_evidence


def test_wrong_route_port_stale_metric_and_forced_partial() -> None:
    items = list(_observations())
    items[0] = _evidence(1, "kubernetes_ingress", "team-a/ingress/front",
                         {"backend_service": "other", "backend_port": 80})
    assert Service503VerifierV2().verify(
        intent=INTENT, evidence=tuple(items), force_partial=False,
    ).status == "PARTIAL"
    items = list(_observations())
    items[3] = _evidence(4, "prometheus_http_503_rate", "team-a/service/api",
                         {"metric_value": 0.3, "metric_unit": "ratio"}, seconds=120)
    assert Service503VerifierV2().verify(
        intent=INTENT, evidence=tuple(items), force_partial=False,
    ).status == "PARTIAL"
    assert Service503VerifierV2().verify(
        intent=INTENT, evidence=_observations(), force_partial=True,
    ).status == "PARTIAL"


def test_ingress_target_and_cross_namespace_evidence() -> None:
    intent = IntentV2(
        domain="microservice", fault_family="service_503",
        target=V2Target(namespace="team-a", kind="ingress", resource="front"),
    )
    assert Service503VerifierV2().verify(
        intent=intent, evidence=_observations(), force_partial=False,
    ).status == "COMPLETED"
    items = (*_observations(), _evidence(
        5, "kubernetes_endpoints", "team-b/service/api",
        {"ready_endpoint_count": 1, "endpoint_snapshot_truncated": False},
    ))
    assert Service503VerifierV2().verify(
        intent=INTENT, evidence=items, force_partial=False,
    ).status == "COMPLETED"


def test_stable_pod_label_mismatch_is_distinct_from_generic_no_ready() -> None:
    items = list(_observations())
    items[1] = _evidence(2, "kubernetes_service", "team-a/service/api", {
        "service_port": 80, "service_port_name": "http", "target_port": 8080,
        "service_type": "ClusterIP", "service_uid": "svc-1",
        "service_resource_version": "12",
    })
    items.append(_evidence(5, "kubernetes_service_membership", "team-a/service/api", {
        "service_uid": "svc-1", "service_resource_version": "12",
        "selector_count": 1, "active_ready_pod_count": 2,
        "matching_ready_pod_count": 0, "membership_truncated": False,
        "rollout_ambiguous": False,
    }))
    assessment = Service503VerifierV2().verify(
        intent=INTENT, evidence=tuple(items), force_partial=False,
    )
    assert assessment.status == "COMPLETED"
    assert assessment.claim.claim_id == "claim_service_503_selector"
    assert len(assessment.claim.evidence_ids) == 5
    for key, value in (("matching_ready_pod_count", 1), ("rollout_ambiguous", True),
                       ("service_resource_version", "13")):
        changed = list(items)
        attrs = {entry.key: entry.value for entry in changed[-1].attributes}
        attrs[key] = value
        changed[-1] = _evidence(5, "kubernetes_service_membership", "team-a/service/api", attrs)
        other = Service503VerifierV2().verify(
            intent=INTENT, evidence=tuple(changed), force_partial=False,
        )
        assert other.claim.claim_id != "claim_service_503_selector"
        if key != "matching_ready_pod_count":
            assert other.status == "PARTIAL"


def test_port_discrepancy_is_reported_without_unproven_503_causality() -> None:
    items = list(_observations())
    items[1] = _evidence(2, "kubernetes_service", "team-a/service/api", {
        "service_port": 80, "service_port_name": "http", "target_port": 8080,
        "service_type": "ClusterIP", "service_uid": "svc-1",
        "service_resource_version": "12",
    })
    items[2] = _evidence(3, "kubernetes_endpoints", "team-a/service/api", {
        "endpoint_count": 1, "ready_endpoint_count": 1,
        "unknown_ready_endpoint_count": 0, "terminating_serving_endpoint_count": 0,
        "endpoint_snapshot_truncated": False,
        "endpoint_ports": '[["http", "TCP", 9999]]',
    })
    items.append(_evidence(5, "kubernetes_service_membership", "team-a/service/api", {
        "service_uid": "svc-1", "service_resource_version": "12",
        "selector_count": 1, "active_ready_pod_count": 1,
        "matching_ready_pod_count": 1, "membership_truncated": False,
        "rollout_ambiguous": False,
    }))
    assessment = Service503VerifierV2().verify(
        intent=INTENT, evidence=tuple(items), force_partial=False,
    )
    assert assessment.status == "PARTIAL"
    assert assessment.claim.claim_id == "claim_service_503_port_anomaly"
    assert assessment.verification.missing_evidence[0].requirement == "port_causality"
    for key, value in (("endpoint_ports", '[["http", "TCP", 8080]]'),
                       ("endpoint_ports", '[["http", "TCP", 9999], ["admin", "TCP", 9000]]')):
        changed = list(items)
        attrs = {entry.key: entry.value for entry in changed[2].attributes}
        attrs[key] = value
        changed[2] = _evidence(3, "kubernetes_endpoints", "team-a/service/api", attrs)
        other = Service503VerifierV2().verify(
            intent=INTENT, evidence=tuple(changed), force_partial=False,
        )
        assert other.claim.claim_id != "claim_service_503_port_anomaly"
