"""Bounded V1 Intent Router."""

from opspilot.routing.models import (
    RouterInput,
    RouterModelOutput,
    RouterOutcome,
    RouterSettings,
)
from opspilot.routing.router import IntentRouter
from opspilot.routing.v2 import (
    IntentV2,
    RouterOutcomeV2,
    V2IntentRouter,
    V2RouterModelOutput,
    V2Target,
)

__all__ = [
    "IntentRouter",
    "RouterInput",
    "RouterModelOutput",
    "RouterOutcome",
    "RouterSettings",
    "IntentV2",
    "RouterOutcomeV2",
    "V2IntentRouter",
    "V2RouterModelOutput",
    "V2Target",
]
