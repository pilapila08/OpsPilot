"""V2 deterministic verifier boundary; concrete fault rules land per work order."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.evidence.models import Claim, Evidence, Verification
from opspilot.routing.v2 import IntentV2


class V2Assessment(StrictSchema):
    status: Literal["COMPLETED", "PARTIAL"]
    root_cause: str = Field(min_length=1, max_length=4_000)
    recommendation: str = Field(min_length=1, max_length=4_000)
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    claim: Claim
    verification: Verification

    @model_validator(mode="after")
    def validate_verdict(self) -> V2Assessment:
        if self.verification.claim_id != self.claim.claim_id:
            raise ValueError("Verification must match V2 claim")
        if (self.status == "COMPLETED") != self.verification.supported:
            raise ValueError("completed assessment requires supported Verification")
        if not set(self.claim.evidence_ids).issubset(self.verification.checked_evidence_ids):
            raise ValueError("V2 claim must cite checked Evidence")
        return self


@runtime_checkable
class V2Verifier(Protocol):
    def verify(
        self, *, intent: IntentV2, evidence: tuple[Evidence, ...],
        force_partial: bool,
    ) -> V2Assessment: ...
