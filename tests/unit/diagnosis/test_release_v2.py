from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from opspilot.diagnosis.release_v2 import PostDeploymentVerifierV2
from opspilot.evidence.models import Evidence, EvidenceAttribute
from opspilot.routing.v2 import IntentV2, V2Target

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
BASE = "a" * 40
HEAD = "b" * 40
INTENT = IntentV2(
    domain="kubernetes", fault_family="post_deployment_failure",
    target=V2Target(namespace="team-a", kind="deployment", resource="api"),
)


def _evidence(
    identifier: str, source: str, minute: int, *, call: str | None = None,
    **attributes: bool | int | float | str,
) -> Evidence:
    return Evidence(
        evidence_id=f"ev_{identifier}", trace_id="trace_release",
        tool_call_id=call or f"tool_{identifier}", source=source,
        resource="team-a/deployment/api", observed_at=NOW + timedelta(minutes=minute),
        collected_at=NOW, content="A bounded release observation.",
        source_confidence=1.0, raw_result_ref=call or f"tool_{identifier}",
        attributes=tuple(EvidenceAttribute(key=key, value=value) for key, value in attributes.items()),
    )


def _release(identifier: str, minute: int, sha: str, status: str = "success") -> Evidence:
    return _evidence(identifier, "cicd_deployment", minute, call="tool_releases",
                     release_id=identifier, commit_sha=sha, release_status=status,
                     environment="staging")


def _metric(identifier: str, minute: int, value: float) -> Evidence:
    return _evidence(identifier, "prometheus_error_rate", minute, call="tool_metrics",
                     metric_value=value, metric_unit="ratio")


def _baseline() -> tuple[Evidence, ...]:
    return (
        _release("release_current", -20, HEAD),
        _release("release_prior", -100, BASE, "inactive"),
        _evidence("commit", "git_commit", -40, commit_sha=HEAD, parent_sha=BASE),
        _evidence("diff", "git_diff", -1, base_sha=BASE, head_sha=HEAD,
                  diff_file_count=2, diff_code_count=2),
        _evidence("deployment", "kubernetes_deployment", -1, generation=2),
        _metric("before_one", -31, 0.001), _metric("before_two", -26, 0.002),
        _metric("after_one", -14, 0.12), _metric("after_two", -10, 0.14),
    )


def _requirements(evidence: tuple[Evidence, ...]) -> set[str]:
    outcome = PostDeploymentVerifierV2().verify(intent=INTENT, evidence=evidence, force_partial=False)
    assert outcome.status == "PARTIAL"
    assert outcome.verification.supported is False
    assert set(outcome.claim.evidence_ids).issubset(outcome.verification.checked_evidence_ids)
    return {item.requirement for item in outcome.verification.missing_evidence}


def test_release_error_rate_timeline_is_correlated_but_never_caused() -> None:
    outcome = PostDeploymentVerifierV2().verify(intent=INTENT, evidence=_baseline(), force_partial=False)
    assert outcome.status == "PARTIAL"
    assert "rose" in outcome.claim.text
    assert outcome.confidence == Decimal("0.4")
    assert {item.requirement for item in outcome.verification.missing_evidence} == {
        "workload_revision_binding", "direct_causality",
    }


def test_error_before_release_is_a_contradiction() -> None:
    evidence = tuple(item for item in _baseline() if item.evidence_id not in {"ev_before_one", "ev_before_two"})
    evidence += (_metric("before_one", -31, 0.1), _metric("before_two", -26, 0.12))
    outcome = PostDeploymentVerifierV2().verify(intent=INTENT, evidence=evidence, force_partial=False)
    assert any("predates" in item.reason for item in outcome.verification.contradictions)
    assert outcome.confidence == 0


def test_overlapping_releases_unknown_commit_and_missing_metric_stay_partial() -> None:
    assert "isolated_release_window" in _requirements(
        _baseline() + (_release("release_overlap", -23, "c" * 40),)
    )
    assert "immutable_commit" in _requirements(tuple(
        item for item in _baseline() if item.source != "git_commit"
    ))
    assert "error_rate_windows" in _requirements(tuple(
        item for item in _baseline() if item.source != "prometheus_error_rate"
    ))


def test_duplicate_metric_samples_and_stale_release_cannot_support_timeline() -> None:
    evidence = tuple(item for item in _baseline() if item.evidence_id != "ev_after_two")
    evidence += (_metric("after_duplicate", -14, 0.14),)
    assert "error_rate_windows" in _requirements(evidence)
    stale = tuple(item.model_copy(update={
        "observed_at": item.observed_at - timedelta(hours=2),
    }) if item.evidence_id == "ev_release_current" else item for item in _baseline())
    assert "fresh_release_window" in _requirements(stale)


def test_rollback_recovery_remains_correlation_only() -> None:
    releases = (
        _release("release_current", -20, BASE),
        _release("release_prior", -100, HEAD, "inactive"),
        _release("release_original", -200, BASE, "inactive"),
    )
    evidence = releases + (
        _evidence("commit", "git_commit", -210, commit_sha=BASE),
        _evidence("deployment", "kubernetes_deployment", -1),
        _metric("before_one", -31, 0.12), _metric("before_two", -26, 0.1),
        _metric("after_one", -14, 0.002), _metric("after_two", -10, 0.001),
    )
    outcome = PostDeploymentVerifierV2().verify(intent=INTENT, evidence=evidence, force_partial=False)
    assert "rollback" in outcome.claim.text
    assert outcome.verification.contradictions == ()
    assert {item.requirement for item in outcome.verification.missing_evidence} == {
        "workload_revision_binding", "direct_causality",
    }


def test_foreign_trace_and_duplicate_evidence_are_rejected() -> None:
    baseline = _baseline()
    with pytest.raises(ValueError, match="unique current-Trace"):
        PostDeploymentVerifierV2().verify(
            intent=INTENT, evidence=baseline + (baseline[0],), force_partial=False,
        )
