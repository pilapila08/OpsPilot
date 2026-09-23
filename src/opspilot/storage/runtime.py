"""SQLAlchemy and in-memory Task, Run and Result repositories."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import cast

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opspilot.agent.state import AgentState, AgentStatus
from opspilot.storage.contracts import (
    ResultSnapshot,
    RunSnapshot,
    RuntimePersistenceError,
    TaskSnapshot,
)
from opspilot.storage.models import (
    AgentRunRecord,
    DiagnosisResultRecord,
    DiagnosisTaskRecord,
)


class SQLAlchemyRuntimeRepository:
    """Persist runtime snapshots without exposing ORM entities to callers."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_task(self, task: TaskSnapshot) -> None:
        self._session.add(
            DiagnosisTaskRecord(
                id=task.task_id,
                user_query=task.user_query,
                namespace=task.namespace,
                status=task.status.value,
            )
        )
        self._commit()

    def get_task(self, task_id: str) -> TaskSnapshot | None:
        row = self._session.get(DiagnosisTaskRecord, task_id)
        if row is None:
            return None
        return TaskSnapshot(
            task_id=row.id,
            user_query=row.user_query,
            namespace=row.namespace,
            status=AgentStatus(row.status),
        )

    def create_run(self, run: RunSnapshot) -> None:
        self._session.add(
            AgentRunRecord(
                id=run.run_id,
                task_id=run.task_id,
                trace_id=run.state.trace_id,
                attempt_no=run.attempt_no,
                status=run.state.status.value,
                state_payload=run.state.model_dump(mode="json"),
                runtime_version="v1",
            )
        )
        self._commit()

    def get_run(self, run_id: str) -> RunSnapshot | None:
        row = self._session.get(AgentRunRecord, run_id)
        if row is None:
            return None
        state = AgentState.model_validate_json(json.dumps(row.state_payload))
        return RunSnapshot(
            run_id=row.id,
            task_id=row.task_id,
            attempt_no=row.attempt_no,
            state=state,
        )

    def save_state(self, run_id: str, state: AgentState) -> None:
        row = self._session.get(AgentRunRecord, run_id)
        if row is None or row.task_id != state.task_id or row.trace_id != state.trace_id:
            raise RuntimePersistenceError("Run does not match AgentState")
        task = self._session.get(DiagnosisTaskRecord, row.task_id)
        if task is None:
            raise RuntimePersistenceError("Run task is missing")
        row.state_payload = state.model_dump(mode="json")
        row.status = state.status.value
        row.updated_at = state.updated_at
        task.status = state.status.value
        task.updated_at = state.updated_at
        if state.status.is_terminal:
            row.completed_at = state.updated_at
            task.completed_at = state.updated_at
        self._commit()

    def append_result(self, result: ResultSnapshot) -> None:
        self._session.add(
            DiagnosisResultRecord(
                id=result.result_id,
                task_id=result.task_id,
                run_id=result.run_id,
                status=result.status,
                root_cause=result.root_cause,
                recommendation=result.recommendation,
                confidence=result.confidence,
                claims_payload=list(result.claims_payload),
                verification_payload=dict(result.verification_payload),
                schema_version=result.schema_version,
            )
        )
        self._commit()

    def get_result(self, run_id: str) -> ResultSnapshot | None:
        row = self._session.scalar(
            select(DiagnosisResultRecord).where(DiagnosisResultRecord.run_id == run_id)
        )
        if row is None:
            return None
        return ResultSnapshot.model_validate(
            {
                "result_id": row.id,
                "task_id": row.task_id,
                "run_id": row.run_id,
                "status": row.status,
                "root_cause": row.root_cause,
                "recommendation": row.recommendation,
                "confidence": Decimal(row.confidence),
                "claims_payload": tuple(cast(dict[str, JsonValue], item) for item in row.claims_payload),
                "verification_payload": cast(dict[str, JsonValue], row.verification_payload),
                "schema_version": row.schema_version,
            }
        )

    def _commit(self) -> None:
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            raise RuntimePersistenceError("runtime record could not be persisted") from None


class InMemoryRuntimeRepository:
    """Deterministic Runtime repository for orchestration tests."""

    def __init__(self) -> None:
        self.tasks: dict[str, TaskSnapshot] = {}
        self.runs: dict[str, RunSnapshot] = {}
        self.results: dict[str, ResultSnapshot] = {}

    def create_task(self, task: TaskSnapshot) -> None:
        if task.task_id in self.tasks:
            raise RuntimePersistenceError("task already exists")
        self.tasks[task.task_id] = task

    def get_task(self, task_id: str) -> TaskSnapshot | None:
        return self.tasks.get(task_id)

    def create_run(self, run: RunSnapshot) -> None:
        if run.run_id in self.runs or run.task_id not in self.tasks:
            raise RuntimePersistenceError("Run already exists or task is missing")
        self.runs[run.run_id] = run

    def get_run(self, run_id: str) -> RunSnapshot | None:
        return self.runs.get(run_id)

    def save_state(self, run_id: str, state: AgentState) -> None:
        run = self.runs.get(run_id)
        if run is None or run.task_id != state.task_id or run.state.trace_id != state.trace_id:
            raise RuntimePersistenceError("Run does not match AgentState")
        self.runs[run_id] = run.model_copy(update={"state": state})
        task = self.tasks[run.task_id]
        self.tasks[run.task_id] = task.model_copy(update={"status": state.status})

    def append_result(self, result: ResultSnapshot) -> None:
        if result.run_id in self.results or result.run_id not in self.runs:
            raise RuntimePersistenceError("result already exists or Run is missing")
        if self.runs[result.run_id].task_id != result.task_id:
            raise RuntimePersistenceError("result task does not match Run")
        self.results[result.run_id] = result

    def get_result(self, run_id: str) -> ResultSnapshot | None:
        return self.results.get(run_id)
