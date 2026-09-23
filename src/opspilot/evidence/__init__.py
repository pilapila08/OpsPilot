"""Evidence-grounded diagnosis contracts."""

from opspilot.evidence.extractors import EvidenceExtractionError, EvidenceExtractorRegistry
from opspilot.evidence.models import (
    Claim,
    Contradiction,
    Evidence,
    EvidenceAttribute,
    MissingEvidence,
    Verification,
)

__all__ = [
    "Claim",
    "Contradiction",
    "Evidence",
    "EvidenceAttribute",
    "EvidenceExtractionError",
    "EvidenceExtractorRegistry",
    "MissingEvidence",
    "Verification",
]
