"""Immutable evidence, claim, and verification contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import StrictSchema

EvidenceId = Annotated[
    str,
    Field(min_length=4, max_length=128, pattern=r"^ev_[a-z0-9][a-z0-9_-]*$"),
]
_CLAIM_ID_PATTERN = r"^claim_[a-z0-9][a-z0-9_-]*$"
_RUNTIME_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
_SOURCE_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
_RESOURCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$"
_ATTRIBUTE_KEY_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"

EvidenceScalar = str | int | float | bool | None


def _unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} cannot contain duplicate IDs")
    return values


class Evidence(StrictSchema):
    """A source-grounded observation derived from one Tool Call."""

    evidence_id: EvidenceId
    trace_id: str = Field(min_length=3, max_length=128, pattern=_RUNTIME_ID_PATTERN)
    tool_call_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=_RUNTIME_ID_PATTERN,
    )
    source: str = Field(min_length=1, max_length=64, pattern=_SOURCE_PATTERN)
    resource: str = Field(
        min_length=1,
        max_length=512,
        pattern=_RESOURCE_PATTERN,
    )
    observed_at: datetime
    collected_at: datetime
    content: str = Field(min_length=1, max_length=20_000)
    source_confidence: float = Field(ge=0, le=1)
    raw_result_ref: str | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=_RUNTIME_ID_PATTERN,
    )
    attributes: tuple[EvidenceAttribute, ...] = ()

    @field_validator("observed_at", "collected_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("resource")
    @classmethod
    def reject_ambiguous_resource_paths(cls, value: str) -> str:
        if ".." in value or "//" in value:
            raise ValueError("resource cannot contain ambiguous path segments")
        return value

    @field_validator("attributes")
    @classmethod
    def require_unique_attribute_keys(
        cls,
        value: tuple[EvidenceAttribute, ...],
    ) -> tuple[EvidenceAttribute, ...]:
        keys = [attribute.key for attribute in value]
        if len(set(keys)) != len(keys):
            raise ValueError("evidence attribute keys must be unique")
        return value

    @model_validator(mode="after")
    def validate_timeline(self) -> Evidence:
        if self.observed_at > self.collected_at:
            raise ValueError("observed_at cannot be later than collected_at")
        return self


class EvidenceAttribute(StrictSchema):
    """Immutable scalar metadata attached to an Evidence summary."""

    key: str = Field(
        min_length=1,
        max_length=64,
        pattern=_ATTRIBUTE_KEY_PATTERN,
    )
    value: EvidenceScalar


class Claim(StrictSchema):
    """A diagnostic assertion explicitly grounded in Evidence IDs."""

    claim_id: str = Field(min_length=7, max_length=128, pattern=_CLAIM_ID_PATTERN)
    text: str = Field(min_length=1, max_length=4_000)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=100)
    inference_confidence: float = Field(ge=0, le=1)

    @field_validator("evidence_ids")
    @classmethod
    def require_unique_evidence_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _unique(value, "evidence_ids")


class MissingEvidence(StrictSchema):
    requirement: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1_000)


class Contradiction(StrictSchema):
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("evidence_ids")
    @classmethod
    def require_unique_evidence_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _unique(value, "evidence_ids")


class Verification(StrictSchema):
    """Verifier result for one Claim and a fixed Evidence set."""

    claim_id: str = Field(min_length=7, max_length=128, pattern=_CLAIM_ID_PATTERN)
    supported: bool
    verification_confidence: float = Field(ge=0, le=1)
    checked_evidence_ids: tuple[EvidenceId, ...] = Field(
        min_length=1,
        max_length=100,
    )
    missing_evidence: tuple[MissingEvidence, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    rationale: str = Field(min_length=1, max_length=4_000)

    @field_validator("checked_evidence_ids")
    @classmethod
    def require_unique_checked_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _unique(value, "checked_evidence_ids")

    @model_validator(mode="after")
    def validate_verdict(self) -> Verification:
        if self.supported and (self.missing_evidence or self.contradictions):
            raise ValueError(
                "supported verification cannot report missing or contradictory evidence"
            )
        if not self.supported and not (
            self.missing_evidence or self.contradictions
        ):
            raise ValueError(
                "unsupported verification must explain missing or contradictory evidence"
            )

        checked_ids = set(self.checked_evidence_ids)
        contradiction_ids = {
            evidence_id
            for contradiction in self.contradictions
            for evidence_id in contradiction.evidence_ids
        }
        if not contradiction_ids.issubset(checked_ids):
            raise ValueError(
                "contradictions can only reference checked Evidence IDs"
            )
        return self
