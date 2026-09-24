"""Append-only V2 round audit independent of frozen V1 plan snapshots."""

from __future__ import annotations

from hashlib import sha256
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opspilot.agent.schemas import StrictSchema
from opspilot.planning.v2 import PlanDecisionV2
from opspilot.storage.models import PlanningRoundRecord


class RoundPersistenceError(RuntimeError):
    pass


class PlanningRoundV2(StrictSchema):
    run_id: str = Field(min_length=3, max_length=128)
    round_no: int = Field(ge=1, le=4)
    prompt_version_id: str = Field(min_length=3, max_length=128)
    decision_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    action: Literal["continue", "finish", "partial"]
    evidence_ids: tuple[str, ...] = Field(max_length=100)
    call_ids: tuple[str, ...] = Field(max_length=4)
    validation_status: Literal["ADMITTED"] = "ADMITTED"

    @model_validator(mode="after")
    def check_action(self) -> PlanningRoundV2:
        if (self.action == "continue") != bool(self.call_ids):
            raise ValueError("round call IDs must match action")
        return self

    @classmethod
    def from_decision(
        cls, *, run_id: str, prompt_version_id: str,
        evidence_ids: tuple[str, ...], decision: PlanDecisionV2,
    ) -> PlanningRoundV2:
        return cls(
            run_id=run_id, round_no=decision.round_no,
            prompt_version_id=prompt_version_id,
            decision_hash=sha256(decision.model_dump_json().encode("utf-8")).hexdigest(),
            action=decision.action, evidence_ids=evidence_ids,
            call_ids=tuple(call.call_id for call in decision.calls),
        )


@runtime_checkable
class PlanningRoundRepository(Protocol):
    def append_round(self, item: PlanningRoundV2) -> None: ...

    def rounds_for_run(self, run_id: str) -> tuple[PlanningRoundV2, ...]: ...


class InMemoryPlanningRoundRepository:
    def __init__(self) -> None:
        self.rounds: list[PlanningRoundV2] = []

    def append_round(self, item: PlanningRoundV2) -> None:
        if any(existing.run_id == item.run_id and existing.round_no == item.round_no for existing in self.rounds):
            raise RoundPersistenceError("planning round already exists")
        self.rounds.append(item)

    def rounds_for_run(self, run_id: str) -> tuple[PlanningRoundV2, ...]:
        return tuple(item for item in self.rounds if item.run_id == run_id)


class SQLAlchemyPlanningRoundRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append_round(self, item: PlanningRoundV2) -> None:
        self._session.add(PlanningRoundRecord(
            run_id=item.run_id, round_no=item.round_no,
            prompt_version_id=item.prompt_version_id,
            decision_hash=item.decision_hash, action=item.action,
            evidence_ids_payload=list(item.evidence_ids),
            call_ids_payload=list(item.call_ids),
            validation_status=item.validation_status,
        ))
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            raise RoundPersistenceError("planning round could not be persisted") from None

    def rounds_for_run(self, run_id: str) -> tuple[PlanningRoundV2, ...]:
        rows = self._session.scalars(
            select(PlanningRoundRecord).where(PlanningRoundRecord.run_id == run_id)
            .order_by(PlanningRoundRecord.round_no)
        ).all()
        return tuple(PlanningRoundV2.model_validate({
            "run_id": row.run_id, "round_no": row.round_no,
            "prompt_version_id": row.prompt_version_id,
            "decision_hash": row.decision_hash, "action": row.action,
            "evidence_ids": tuple(row.evidence_ids_payload),
            "call_ids": tuple(row.call_ids_payload),
            "validation_status": row.validation_status,
        }) for row in rows)
