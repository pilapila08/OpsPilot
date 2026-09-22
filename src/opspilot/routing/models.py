"""Strict contracts for the bounded V1 Intent Router."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opspilot.agent.schemas import (
    BudgetState,
    IntentOutput,
    StrictSchema,
)
from opspilot.integrations.kubernetes.models import NamespaceName, ResourceName

_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
_CLASSIFICATION_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class RouterInput(StrictSchema):
    run_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    query: str = Field(min_length=1, max_length=4_000)
    namespace: NamespaceName


class RouterModelOutput(StrictSchema):
    """Model-controlled fields; namespace is deliberately absent."""

    intent: str = Field(
        min_length=1,
        max_length=64,
        pattern=_CLASSIFICATION_PATTERN,
    )
    domain: str = Field(
        min_length=1,
        max_length=64,
        pattern=_CLASSIFICATION_PATTERN,
    )
    problem_type: str = Field(
        min_length=1,
        max_length=64,
        pattern=_CLASSIFICATION_PATTERN,
    )
    resource: ResourceName


class RouterSettings(StrictSchema):
    schema_version: Literal[1] = 1
    max_schema_retries: int = Field(default=2, ge=0, le=2)


class RouterOutcome(StrictSchema):
    intent: IntentOutput
    budget: BudgetState
    attempts: int = Field(ge=1, le=3)
    llm_call_ids: tuple[str, ...] = Field(min_length=1, max_length=3)
