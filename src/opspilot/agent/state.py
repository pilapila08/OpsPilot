"""Explicit, auditable state transitions for a diagnosis run."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import BudgetState, IntentOutput, Plan, StrictSchema

_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"


class AgentStatus(StrEnum):
    CREATED = "CREATED"
    ROUTING = "ROUTING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    POLICY_REJECTED = "POLICY_REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            AgentStatus.COMPLETED,
            AgentStatus.PARTIAL,
            AgentStatus.FAILED,
            AgentStatus.BUDGET_EXCEEDED,
            AgentStatus.POLICY_REJECTED,
        }


class InvalidStateTransition(ValueError):
    """Raised when a transition violates the runtime state machine."""


class StateTransition(StrictSchema):
    from_status: AgentStatus
    to_status: AgentStatus
    transitioned_at: datetime

    @field_validator("transitioned_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("transitioned_at must be timezone-aware")
        return value.astimezone(UTC)


class AgentState(StrictSchema):
    """Immutable snapshot of one diagnostic run."""

    _ALLOWED_TRANSITIONS: ClassVar[Mapping[AgentStatus, frozenset[AgentStatus]]] = {
        AgentStatus.CREATED: frozenset(
            {
                AgentStatus.ROUTING,
                AgentStatus.FAILED,
                AgentStatus.POLICY_REJECTED,
                AgentStatus.BUDGET_EXCEEDED,
            }
        ),
        AgentStatus.ROUTING: frozenset(
            {
                AgentStatus.PLANNING,
                AgentStatus.FAILED,
                AgentStatus.POLICY_REJECTED,
                AgentStatus.BUDGET_EXCEEDED,
            }
        ),
        AgentStatus.PLANNING: frozenset(
            {
                AgentStatus.EXECUTING,
                AgentStatus.FAILED,
                AgentStatus.PARTIAL,
                AgentStatus.POLICY_REJECTED,
                AgentStatus.BUDGET_EXCEEDED,
            }
        ),
        AgentStatus.EXECUTING: frozenset(
            {
                AgentStatus.PLANNING,
                AgentStatus.VERIFYING,
                AgentStatus.WAITING_APPROVAL,
                AgentStatus.PARTIAL,
                AgentStatus.FAILED,
                AgentStatus.POLICY_REJECTED,
                AgentStatus.BUDGET_EXCEEDED,
            }
        ),
        AgentStatus.VERIFYING: frozenset(
            {
                AgentStatus.EXECUTING,
                AgentStatus.COMPLETED,
                AgentStatus.PARTIAL,
                AgentStatus.FAILED,
                AgentStatus.POLICY_REJECTED,
                AgentStatus.BUDGET_EXCEEDED,
            }
        ),
        AgentStatus.WAITING_APPROVAL: frozenset(
            {
                AgentStatus.EXECUTING,
                AgentStatus.PARTIAL,
                AgentStatus.FAILED,
                AgentStatus.POLICY_REJECTED,
                AgentStatus.BUDGET_EXCEEDED,
            }
        ),
        AgentStatus.COMPLETED: frozenset(),
        AgentStatus.PARTIAL: frozenset(),
        AgentStatus.FAILED: frozenset(),
        AgentStatus.BUDGET_EXCEEDED: frozenset(),
        AgentStatus.POLICY_REJECTED: frozenset(),
    }

    task_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    trace_id: str = Field(min_length=3, max_length=128, pattern=_ID_PATTERN)
    user_query: str = Field(min_length=1, max_length=4_000)
    intent: IntentOutput | None = None
    plan: Plan | None = None
    current_step: int = Field(default=0, ge=0)
    tool_call_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    diagnosis_id: str | None = None
    verification_id: str | None = None
    budget: BudgetState = Field(default_factory=BudgetState)
    status: AgentStatus = AgentStatus.CREATED
    transitions: tuple[StateTransition, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("state timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_state_snapshot(self) -> AgentState:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        if self.plan is None and self.current_step != 0:
            raise ValueError("current_step must be 0 when no plan exists")
        if self.plan is not None and self.current_step > len(self.plan.steps):
            raise ValueError("current_step cannot exceed the plan length")
        return self

    def transition_to(
        self,
        target: AgentStatus,
        *,
        at: datetime | None = None,
    ) -> AgentState:
        """Return a new state snapshot after a validated transition."""

        if target not in self._ALLOWED_TRANSITIONS[self.status]:
            raise InvalidStateTransition(
                f"transition from {self.status.value} to {target.value} is not allowed"
            )

        transitioned_at = at or datetime.now(UTC)
        if transitioned_at.tzinfo is None or transitioned_at.utcoffset() is None:
            raise ValueError("transition time must be timezone-aware")
        transitioned_at = transitioned_at.astimezone(UTC)
        if transitioned_at < self.updated_at:
            raise ValueError("transition time cannot be earlier than updated_at")

        event = StateTransition(
            from_status=self.status,
            to_status=target,
            transitioned_at=transitioned_at,
        )
        return self.model_copy(
            update={
                "status": target,
                "updated_at": transitioned_at,
                "transitions": (*self.transitions, event),
            }
        )

