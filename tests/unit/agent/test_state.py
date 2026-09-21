from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from opspilot.agent.state import (
    AgentState,
    AgentStatus,
    InvalidStateTransition,
)


def make_state() -> AgentState:
    created_at = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    return AgentState(
        task_id="task_001",
        trace_id="trace_001",
        user_query="Why does payment-service keep restarting?",
        created_at=created_at,
        updated_at=created_at,
    )


def test_state_round_trip_json() -> None:
    state = make_state().transition_to(
        AgentStatus.ROUTING,
        at=datetime(2026, 9, 21, 10, 1, tzinfo=UTC),
    )

    restored = AgentState.model_validate_json(state.model_dump_json())

    assert restored == state


def test_valid_transition_returns_new_snapshot_and_audit_event() -> None:
    state = make_state()
    transitioned_at = state.updated_at + timedelta(seconds=1)

    routed = state.transition_to(AgentStatus.ROUTING, at=transitioned_at)

    assert state.status is AgentStatus.CREATED
    assert state.transitions == ()
    assert routed.status is AgentStatus.ROUTING
    assert routed.updated_at == transitioned_at
    assert routed.transitions[0].from_status is AgentStatus.CREATED
    assert routed.transitions[0].to_status is AgentStatus.ROUTING


def test_dynamic_diagnosis_loop_is_allowed() -> None:
    state = make_state()
    base_time = state.updated_at

    state = state.transition_to(AgentStatus.ROUTING, at=base_time)
    state = state.transition_to(AgentStatus.PLANNING, at=base_time)
    state = state.transition_to(AgentStatus.EXECUTING, at=base_time)
    state = state.transition_to(AgentStatus.PLANNING, at=base_time)

    assert state.status is AgentStatus.PLANNING
    assert len(state.transitions) == 4


def test_invalid_transition_is_rejected() -> None:
    state = make_state()

    with pytest.raises(InvalidStateTransition):
        state.transition_to(AgentStatus.COMPLETED, at=state.updated_at)


@pytest.mark.parametrize(
    "terminal_status",
    [
        AgentStatus.COMPLETED,
        AgentStatus.PARTIAL,
        AgentStatus.FAILED,
        AgentStatus.BUDGET_EXCEEDED,
        AgentStatus.POLICY_REJECTED,
    ],
)
def test_terminal_state_cannot_transition(terminal_status: AgentStatus) -> None:
    state = make_state().model_copy(update={"status": terminal_status})

    with pytest.raises(InvalidStateTransition):
        state.transition_to(AgentStatus.ROUTING, at=state.updated_at)


def test_transition_rejects_naive_or_older_time() -> None:
    state = make_state()

    with pytest.raises(ValueError, match="timezone-aware"):
        state.transition_to(AgentStatus.ROUTING, at=datetime(2026, 9, 21, 10, 1))

    with pytest.raises(ValueError, match="earlier"):
        state.transition_to(
            AgentStatus.ROUTING,
            at=state.updated_at - timedelta(seconds=1),
        )


def test_state_rejects_current_step_without_plan() -> None:
    with pytest.raises(ValidationError):
        AgentState(
            task_id="task_001",
            trace_id="trace_001",
            user_query="Why is the pod restarting?",
            current_step=1,
        )


def test_state_is_immutable() -> None:
    state = make_state()

    with pytest.raises(ValidationError):
        state.status = AgentStatus.ROUTING

