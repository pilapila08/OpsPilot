"""Flat provider envelope for domain models with dynamic or nested JSON."""

from __future__ import annotations

from pydantic import Field

from opspilot.agent.schemas import StrictSchema


class StrictJsonEnvelope(StrictSchema):
    """The SDK validates this flat object; the adapter validates its JSON payload."""

    payload_json: str = Field(min_length=2, max_length=50_000)
