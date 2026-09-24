"""Read one complete audit chain without exposing stored raw payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
from typing import Literal

from pydantic import Field
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from opspilot.agent.schemas import StrictSchema
from opspilot.agent.state import AgentStatus
from opspilot.execution.repository import ToolAttemptView
from opspilot.llm.audit import ModelCallView
from opspilot.storage.execution import SQLAlchemyExecutionRepository
from opspilot.storage.model_audit import SQLAlchemyModelAuditRepository
from opspilot.storage.models import AgentRunRecord, DiagnosisTaskRecord
from opspilot.storage.planning_rounds import (
    PlanningRoundV2,
    SQLAlchemyPlanningRoundRepository,
)
from opspilot.storage.runtime import SQLAlchemyRuntimeRepository
from opspilot.storage.contracts import BudgetStopSnapshot, ResultSnapshot

_TRACE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")


class TraceDatabaseUnavailable(RuntimeError):
    """A local database cannot be queried without creating one."""


class TaskTraceView(StrictSchema):
    task_id: str
    namespace: str | None
    mode: str | None
    case_id: str | None


class RunTraceView(StrictSchema):
    run_id: str
    trace_id: str
    task_id: str
    attempt_no: int
    runtime_version: Literal["v1", "v2"]
    status: AgentStatus
    planning_mode: Literal["single-pass", "multi-round"]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class EvidenceTraceView(StrictSchema):
    evidence_id: str
    tool_call_id: str
    source: str
    resource: str
    observed_at: datetime
    collected_at: datetime
    source_confidence: float


class VerificationTraceView(StrictSchema):
    kind: Literal["budget_stop"] | None = None
    supported: bool | None
    checked_evidence_ids: tuple[str, ...]
    missing_evidence_count: int = Field(ge=0)
    contradiction_count: int = Field(ge=0)


class ResultTraceView(StrictSchema):
    result_id: str
    status: Literal["COMPLETED", "PARTIAL"]
    schema_version: int
    root_cause: str
    recommendation: str
    confidence: Decimal
    claim_ids: tuple[str, ...]
    cited_evidence_ids: tuple[str, ...]
    verification: VerificationTraceView


class TraceStatistics(StrictSchema):
    planning_rounds: int = Field(ge=0)
    budget_stops: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    failed_model_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    model_cost_usd: Decimal = Field(ge=Decimal("0"))
    tool_attempts: int = Field(ge=0)
    failed_tool_attempts: int = Field(ge=0)
    tool_retries: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    budget_steps_used: int = Field(ge=0)
    budget_tool_calls_used: int = Field(ge=0)
    budget_retries_used: int = Field(ge=0)
    budget_tokens_used: int = Field(ge=0)
    budget_cost_usd: Decimal = Field(ge=Decimal("0"))
    budget_elapsed_seconds: float = Field(ge=0)


class TraceView(StrictSchema):
    task: TaskTraceView
    run: RunTraceView
    rounds: tuple[PlanningRoundV2, ...]
    budget_stops: tuple[BudgetStopSnapshot, ...]
    model_calls: tuple[ModelCallView, ...]
    tool_attempts: tuple[ToolAttemptView, ...]
    evidence: tuple[EvidenceTraceView, ...]
    result: ResultTraceView | None
    statistics: TraceStatistics


class TraceQueryService:
    """Aggregate existing repository reads within one read-only Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, trace_id: str) -> TraceView | None:
        if _TRACE_ID.fullmatch(trace_id) is None:
            return None
        run_row = self._session.scalar(
            select(AgentRunRecord).where(AgentRunRecord.trace_id == trace_id)
        )
        if run_row is None:
            return None
        task_row = self._session.get(DiagnosisTaskRecord, run_row.task_id)
        if task_row is None:
            return None
        runtime = SQLAlchemyRuntimeRepository(self._session)
        run = runtime.get_run(run_row.id)
        if run is None or run.state.trace_id != trace_id:
            return None
        rounds = SQLAlchemyPlanningRoundRepository(self._session).rounds_for_run(run.run_id)
        budget_stops = runtime.budget_stops_for_trace(trace_id)
        model_calls = SQLAlchemyModelAuditRepository(self._session).calls_for_trace(trace_id)
        execution = SQLAlchemyExecutionRepository(self._session)
        tool_attempts = execution.attempts_for_trace(trace_id)
        evidence = execution.evidence_for_trace(trace_id)
        result = runtime.get_result(run.run_id)
        usage = run.state.budget
        return TraceView(
            task=TaskTraceView(
                task_id=task_row.id,
                namespace=task_row.namespace,
                mode=task_row.mode,
                case_id=task_row.case_id,
            ),
            run=RunTraceView(
                run_id=run.run_id,
                trace_id=trace_id,
                task_id=run.task_id,
                attempt_no=run.attempt_no,
                runtime_version=run.runtime_version,
                status=run.state.status,
                planning_mode="single-pass" if run.runtime_version == "v1" else "multi-round",
                started_at=_aware(run_row.started_at),
                updated_at=_aware(run_row.updated_at),
                completed_at=_aware(run_row.completed_at) if run_row.completed_at else None,
            ),
            rounds=rounds,
            budget_stops=budget_stops,
            model_calls=model_calls,
            tool_attempts=tool_attempts,
            evidence=tuple(EvidenceTraceView(
                evidence_id=item.evidence_id,
                tool_call_id=item.tool_call_id,
                source=item.source,
                resource=item.resource,
                observed_at=item.observed_at,
                collected_at=item.collected_at,
                source_confidence=item.source_confidence,
            ) for item in evidence),
            result=_result_view(result) if result is not None else None,
            statistics=TraceStatistics(
                planning_rounds=len(rounds),
                budget_stops=len(budget_stops),
                model_attempts=len(model_calls),
                failed_model_attempts=sum(not item.success for item in model_calls),
                input_tokens=sum(item.input_tokens for item in model_calls),
                output_tokens=sum(item.output_tokens for item in model_calls),
                model_cost_usd=sum((item.cost_usd for item in model_calls), Decimal("0")),
                tool_attempts=len(tool_attempts),
                failed_tool_attempts=sum(not item.success for item in tool_attempts),
                tool_retries=sum(item.attempt_no > 1 for item in tool_attempts),
                evidence_count=len(evidence),
                budget_steps_used=usage.steps_used,
                budget_tool_calls_used=usage.tool_calls_used,
                budget_retries_used=usage.retries_used,
                budget_tokens_used=usage.tokens_used,
                budget_cost_usd=usage.cost_usd,
                budget_elapsed_seconds=usage.elapsed_seconds,
            ),
        )


def query_trace(database_url: str, trace_id: str) -> TraceView | None:
    """Open only an existing database and read a Trace; never run migrations."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        database = url.database
        if not database or database == ":memory:" or not Path(database).is_file():
            raise TraceDatabaseUnavailable("diagnosis database does not exist")
        sqlite_path = Path(database).resolve(strict=True)
        # SQLite's read-only URI remains safe if the file disappears after the
        # existence check: it fails instead of silently creating a new DB.
        engine: Engine = create_engine(
            "sqlite://",
            creator=lambda: sqlite3.connect(
                f"{sqlite_path.as_uri()}?mode=ro", uri=True,
            ),
        )
    else:
        engine = create_engine(url)
    try:
        with Session(engine) as session:
            return TraceQueryService(session).get(trace_id)
    finally:
        engine.dispose()


def _result_view(result: ResultSnapshot) -> ResultTraceView:
    claim_ids: list[str] = []
    cited_ids: list[str] = []
    for claim in result.claims_payload:
        claim_id = claim.get("claim_id")
        if isinstance(claim_id, str):
            claim_ids.append(claim_id)
        evidence_ids = claim.get("evidence_ids")
        if isinstance(evidence_ids, list):
            cited_ids.extend(item for item in evidence_ids if isinstance(item, str))
    verification = result.verification_payload
    checked = verification.get("checked_evidence_ids")
    missing = verification.get("missing_evidence")
    contradictions = verification.get("contradictions")
    supported = verification.get("supported")
    return ResultTraceView(
        result_id=result.result_id,
        status=result.status,
        schema_version=result.schema_version,
        root_cause=result.root_cause,
        recommendation=result.recommendation,
        confidence=result.confidence,
        claim_ids=tuple(claim_ids),
        cited_evidence_ids=tuple(dict.fromkeys(cited_ids)),
        verification=VerificationTraceView(
            kind="budget_stop" if result.schema_version == 3 else None,
            supported=supported if isinstance(supported, bool) else None,
            checked_evidence_ids=tuple(
                item for item in checked if isinstance(item, str)
            ) if isinstance(checked, list) else (),
            missing_evidence_count=len(missing) if isinstance(missing, list) else 0,
            contradiction_count=len(contradictions) if isinstance(contradictions, list) else 0,
        ),
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
