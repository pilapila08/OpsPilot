from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from opspilot.evidence import (
    Claim,
    Contradiction,
    Evidence,
    EvidenceAttribute,
    MissingEvidence,
    Verification,
)


def make_evidence(**changes: object) -> Evidence:
    observed_at = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    values: dict[str, object] = {
        "evidence_id": "ev_001",
        "trace_id": "trace_001",
        "tool_call_id": "call_001",
        "source": "kubernetes_events",
        "resource": "prod/payment-service",
        "observed_at": observed_at,
        "collected_at": observed_at + timedelta(seconds=1),
        "content": "Liveness probe failed",
        "source_confidence": 1.0,
        "raw_result_ref": "result_001",
        "attributes": (
            EvidenceAttribute(key="reason", value="Unhealthy"),
        ),
    }
    values.update(changes)
    return Evidence.model_validate(values)


def test_evidence_round_trips_with_traceability_fields() -> None:
    evidence = make_evidence()

    restored = Evidence.model_validate_json(evidence.model_dump_json())

    assert restored == evidence
    assert restored.trace_id == "trace_001"
    assert restored.tool_call_id == "call_001"
    assert restored.raw_result_ref == "result_001"


def test_boolean_evidence_attribute_survives_json_round_trip() -> None:
    evidence = make_evidence(
        attributes=(EvidenceAttribute(key="startup_probe_configured", value=False),)
    )
    restored = Evidence.model_validate_json(evidence.model_dump_json())

    assert restored.attributes[0].value is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_id", "invalid"),
        ("trace_id", ""),
        ("tool_call_id", ""),
        ("resource", "../../etc/passwd"),
        ("source_confidence", -0.01),
        ("source_confidence", 1.01),
    ],
)
def test_evidence_rejects_invalid_identity_or_confidence(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        make_evidence(**{field: value})


def test_evidence_rejects_naive_or_reversed_timestamps() -> None:
    observed_at = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="timezone-aware"):
        make_evidence(observed_at=datetime(2026, 9, 22, 10, 0))

    with pytest.raises(ValidationError, match="later"):
        make_evidence(
            observed_at=observed_at + timedelta(seconds=2),
            collected_at=observed_at,
        )


def test_evidence_is_immutable() -> None:
    evidence = make_evidence()

    with pytest.raises(ValidationError):
        setattr(evidence, "content", "changed")

    with pytest.raises(ValidationError):
        setattr(evidence.attributes[0], "value", "Changed")


def test_evidence_rejects_duplicate_attribute_keys() -> None:
    with pytest.raises(ValidationError):
        make_evidence(
            attributes=(
                EvidenceAttribute(key="reason", value="Unhealthy"),
                EvidenceAttribute(key="reason", value="ProbeFailed"),
            )
        )


def test_claim_requires_unique_evidence_and_separate_inference_confidence() -> None:
    claim = Claim(
        claim_id="claim_001",
        text="Liveness Probe starts before the application is ready",
        evidence_ids=("ev_001", "ev_002"),
        inference_confidence=0.91,
    )

    assert claim.inference_confidence == 0.91
    assert "source_confidence" not in claim.model_fields

    with pytest.raises(ValidationError):
        Claim(
            claim_id="claim_002",
            text="Duplicate evidence",
            evidence_ids=("ev_001", "ev_001"),
            inference_confidence=0.5,
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_claim_rejects_invalid_inference_confidence(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Claim(
            claim_id="claim_001",
            text="Unsupported confidence",
            evidence_ids=("ev_001",),
            inference_confidence=confidence,
        )


def test_claim_requires_at_least_one_evidence_reference() -> None:
    with pytest.raises(ValidationError):
        Claim(
            claim_id="claim_001",
            text="Ungrounded claim",
            evidence_ids=(),
            inference_confidence=0.5,
        )


def test_supported_verification_has_no_missing_or_contradictory_evidence() -> None:
    verification = Verification(
        claim_id="claim_001",
        supported=True,
        verification_confidence=0.95,
        checked_evidence_ids=("ev_001", "ev_002"),
        rationale="Both probe configuration and events support the claim.",
    )

    assert verification.supported is True
    assert verification.missing_evidence == ()
    assert verification.contradictions == ()


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_verification_rejects_invalid_confidence(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Verification(
            claim_id="claim_001",
            supported=True,
            verification_confidence=confidence,
            checked_evidence_ids=("ev_001",),
            rationale="Confidence must remain within the closed unit interval.",
        )


def test_supported_verification_rejects_reported_problems() -> None:
    with pytest.raises(ValidationError):
        Verification(
            claim_id="claim_001",
            supported=True,
            verification_confidence=0.7,
            checked_evidence_ids=("ev_001",),
            missing_evidence=(
                MissingEvidence(
                    requirement="startup duration",
                    reason="No startup timing was collected",
                ),
            ),
            rationale="Evidence is incomplete.",
        )


def test_unsupported_verification_requires_an_explanation() -> None:
    with pytest.raises(ValidationError):
        Verification(
            claim_id="claim_001",
            supported=False,
            verification_confidence=0.8,
            checked_evidence_ids=("ev_001",),
            rationale="The claim is not supported.",
        )


def test_contradiction_must_reference_checked_evidence() -> None:
    with pytest.raises(ValidationError):
        Verification(
            claim_id="claim_001",
            supported=False,
            verification_confidence=0.8,
            checked_evidence_ids=("ev_001",),
            contradictions=(
                Contradiction(
                    evidence_ids=("ev_002",),
                    reason="The startup probe already protects initialization.",
                ),
            ),
            rationale="An unchecked record cannot establish a contradiction.",
        )


def test_unsupported_verification_can_report_missing_and_conflicting_evidence() -> None:
    verification = Verification(
        claim_id="claim_001",
        supported=False,
        verification_confidence=0.88,
        checked_evidence_ids=("ev_001", "ev_002"),
        missing_evidence=(
            MissingEvidence(
                requirement="previous container logs",
                reason="The previous log stream was unavailable",
            ),
        ),
        contradictions=(
            Contradiction(
                evidence_ids=("ev_002",),
                reason="Deployment includes a valid startup probe.",
            ),
        ),
        rationale="Available evidence conflicts with the proposed root cause.",
    )

    assert verification.supported is False
    assert verification.verification_confidence == 0.88
