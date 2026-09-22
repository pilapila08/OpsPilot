"""SQLAlchemy models for the auditable OpsPilot runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class DiagnosisTaskRecord(Base):
    __tablename__ = "diagnosis_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED', 'ROUTING', 'PLANNING', 'EXECUTING', "
            "'VERIFYING', 'WAITING_APPROVAL', 'COMPLETED', 'PARTIAL', "
            "'FAILED', 'BUDGET_EXCEEDED', 'POLICY_REJECTED')",
            name="status_valid",
        ),
        Index("ix_diagnosis_tasks_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(63))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    runs: Mapped[list[AgentRunRecord]] = relationship(
        back_populates="task",
        passive_deletes=True,
    )
    results: Mapped[list[DiagnosisResultRecord]] = relationship(
        back_populates="task",
        viewonly=True,
    )


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no"),
        UniqueConstraint("id", "task_id"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(
            "status IN ('CREATED', 'ROUTING', 'PLANNING', 'EXECUTING', "
            "'VERIFYING', 'WAITING_APPROVAL', 'COMPLETED', 'PARTIAL', "
            "'FAILED', 'BUDGET_EXCEEDED', 'POLICY_REJECTED')",
            name="status_valid",
        ),
        Index("ix_agent_runs_task_status", "task_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trace_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    runtime_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[DiagnosisTaskRecord] = relationship(back_populates="runs")
    llm_calls: Mapped[list[LLMCallRecord]] = relationship(
        back_populates="run",
        passive_deletes=True,
    )
    tool_calls: Mapped[list[ToolCallRecord]] = relationship(
        back_populates="run",
        passive_deletes=True,
    )
    evidence: Mapped[list[EvidenceRecord]] = relationship(
        back_populates="run",
        viewonly=True,
    )
    result: Mapped[DiagnosisResultRecord | None] = relationship(
        back_populates="run",
        passive_deletes=True,
    )


class PromptVersionRecord(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("component", "version"),
        Index("ix_prompt_versions_component_created_at", "component", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    model_family: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    llm_calls: Mapped[list[LLMCallRecord]] = relationship(
        back_populates="prompt_version",
        passive_deletes=True,
    )


class LLMCallRecord(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no"),
        CheckConstraint("sequence_no >= 1", name="sequence_no_positive"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint("cost_usd >= 0", name="cost_usd_nonnegative"),
        CheckConstraint("latency_ms >= 0", name="latency_ms_nonnegative"),
        CheckConstraint("retry_count >= 0", name="retry_count_nonnegative"),
        Index("ix_llm_calls_run_started_at", "run_id", "started_at"),
        Index("ix_llm_calls_model", "provider", "model_name"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    prompt_version_id: Mapped[str] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), nullable=False
    )
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRunRecord] = relationship(back_populates="llm_calls")
    prompt_version: Mapped[PromptVersionRecord] = relationship(
        back_populates="llm_calls"
    )


class ToolCallRecord(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no"),
        UniqueConstraint("id", "run_id"),
        CheckConstraint("sequence_no >= 1", name="sequence_no_positive"),
        CheckConstraint("risk_level BETWEEN 0 AND 2", name="risk_level_valid"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_nonnegative",
        ),
        Index("ix_tool_calls_run_started_at", "run_id", "started_at"),
        Index("ix_tool_calls_name_success", "tool_name", "success"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    arguments_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool | None] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRunRecord] = relationship(back_populates="tool_calls")
    evidence: Mapped[list[EvidenceRecord]] = relationship(
        back_populates="tool_call",
        passive_deletes=True,
    )


class EvidenceRecord(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tool_call_id", "run_id"],
            ["tool_calls.id", "tool_calls.run_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_confidence >= 0 AND source_confidence <= 1",
            name="source_confidence_valid",
        ),
        CheckConstraint(
            "observed_at <= collected_at",
            name="timeline_valid",
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        Index("ix_evidence_run_source", "run_id", "source"),
        Index("ix_evidence_resource_observed_at", "resource", "observed_at"),
        Index("ix_evidence_tool_call_id", "tool_call_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    raw_result_ref: Mapped[str | None] = mapped_column(String(128))
    attributes_payload: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    run: Mapped[AgentRunRecord] = relationship(
        back_populates="evidence",
        viewonly=True,
    )
    tool_call: Mapped[ToolCallRecord] = relationship(back_populates="evidence")


class DiagnosisResultRecord(Base):
    __tablename__ = "diagnosis_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["agent_runs.id", "agent_runs.task_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id"),
        CheckConstraint(
            "status IN ('COMPLETED', 'PARTIAL')",
            name="status_valid",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_valid",
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        Index("ix_diagnosis_results_task_created_at", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    claims_payload: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    verification_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    task: Mapped[DiagnosisTaskRecord] = relationship(
        back_populates="results",
        viewonly=True,
    )
    run: Mapped[AgentRunRecord] = relationship(back_populates="result")


class EvidenceMutationError(RuntimeError):
    """Raised when application code tries to mutate append-only Evidence."""


def _prevent_evidence_update(
    mapper: Mapper[Any],
    connection: Connection,
    target: EvidenceRecord,
) -> None:
    del mapper, connection, target
    raise EvidenceMutationError("Evidence records are append-only")


def _prevent_evidence_delete(
    mapper: Mapper[Any],
    connection: Connection,
    target: EvidenceRecord,
) -> None:
    del mapper, connection, target
    raise EvidenceMutationError("Evidence records cannot be deleted")


event.listen(EvidenceRecord, "before_update", _prevent_evidence_update)
event.listen(EvidenceRecord, "before_delete", _prevent_evidence_delete)
