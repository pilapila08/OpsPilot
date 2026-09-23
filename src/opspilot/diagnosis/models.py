"""Versioned candidate and verified V1 diagnosis contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator

from opspilot.agent.schemas import BudgetState, StrictSchema
from opspilot.evidence.models import Claim, Verification


class DiagnosisDraftV1(StrictSchema):
    schema_version: Literal[1]
    root_cause: str = Field(min_length=1, max_length=4_000)
    recommendation: str = Field(min_length=1, max_length=4_000)
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=1)

    @field_validator("claims")
    @classmethod
    def unique_claim_ids(cls, claims: tuple[Claim, ...]) -> tuple[Claim, ...]:
        if len({claim.claim_id for claim in claims}) != len(claims):
            raise ValueError("diagnosis claim IDs must be unique")
        return claims


class DiagnosisAssessment(StrictSchema):
    status: Literal["COMPLETED", "PARTIAL"]
    root_cause: str = Field(min_length=1, max_length=4_000)
    recommendation: str = Field(min_length=1, max_length=4_000)
    claim: Claim
    verification: Verification
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))


class DiagnosisOutcome(StrictSchema):
    assessment: DiagnosisAssessment
    budget: BudgetState
    llm_call_ids: tuple[str, ...]
    result_id: str
