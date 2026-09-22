"""Create the auditable runtime core schema.

Revision ID: 20260922_0001
Revises: None
Create Date: 2026-09-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260922_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diagnosis_tasks",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("user_query", sa.Text(), nullable=False),
        sa.Column("namespace", sa.String(length=63), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('CREATED', 'ROUTING', 'PLANNING', 'EXECUTING', "
            "'VERIFYING', 'WAITING_APPROVAL', 'COMPLETED', 'PARTIAL', "
            "'FAILED', 'BUDGET_EXCEEDED', 'POLICY_REJECTED')",
            name="ck_diagnosis_tasks_status_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_diagnosis_tasks"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_diagnosis_tasks_idempotency_key"
        ),
    )
    op.create_index(
        "ix_diagnosis_tasks_status_created_at",
        "diagnosis_tasks",
        ["status", "created_at"],
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_payload", sa.JSON(), nullable=False),
        sa.Column("runtime_version", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_no >= 1", name="ck_agent_runs_attempt_no_positive"),
        sa.CheckConstraint(
            "status IN ('CREATED', 'ROUTING', 'PLANNING', 'EXECUTING', "
            "'VERIFYING', 'WAITING_APPROVAL', 'COMPLETED', 'PARTIAL', "
            "'FAILED', 'BUDGET_EXCEEDED', 'POLICY_REJECTED')",
            name="ck_agent_runs_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["diagnosis_tasks.id"],
            name="fk_agent_runs_task_id_diagnosis_tasks",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.UniqueConstraint("id", "task_id", name="uq_agent_runs_id_task_id"),
        sa.UniqueConstraint(
            "task_id", "attempt_no", name="uq_agent_runs_task_id_attempt_no"
        ),
        sa.UniqueConstraint("trace_id", name="uq_agent_runs_trace_id"),
    )
    op.create_index(
        "ix_agent_runs_task_status", "agent_runs", ["task_id", "status"]
    )

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("model_family", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_prompt_versions"),
        sa.UniqueConstraint(
            "component", "version", name="uq_prompt_versions_component_version"
        ),
    )
    op.create_index(
        "ix_prompt_versions_component_created_at",
        "prompt_versions",
        ["component", "created_at"],
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_version_id", sa.String(length=128), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False
        ),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("cost_usd >= 0", name="ck_llm_calls_cost_usd_nonnegative"),
        sa.CheckConstraint(
            "input_tokens >= 0", name="ck_llm_calls_input_tokens_nonnegative"
        ),
        sa.CheckConstraint(
            "latency_ms >= 0", name="ck_llm_calls_latency_ms_nonnegative"
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name="ck_llm_calls_output_tokens_nonnegative"
        ),
        sa.CheckConstraint(
            "retry_count >= 0", name="ck_llm_calls_retry_count_nonnegative"
        ),
        sa.CheckConstraint(
            "sequence_no >= 1", name="ck_llm_calls_sequence_no_positive"
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
            name="fk_llm_calls_prompt_version_id_prompt_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_llm_calls_run_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
        sa.UniqueConstraint(
            "run_id", "sequence_no", name="uq_llm_calls_run_id_sequence_no"
        ),
    )
    op.create_index("ix_llm_calls_model", "llm_calls", ["provider", "model_name"])
    op.create_index(
        "ix_llm_calls_run_started_at", "llm_calls", ["run_id", "started_at"]
    )

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("risk_level", sa.Integer(), nullable=False),
        sa.Column("arguments_payload", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_tool_calls_duration_ms_nonnegative",
        ),
        sa.CheckConstraint(
            "risk_level BETWEEN 0 AND 2", name="ck_tool_calls_risk_level_valid"
        ),
        sa.CheckConstraint(
            "sequence_no >= 1", name="ck_tool_calls_sequence_no_positive"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_tool_calls_run_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_calls"),
        sa.UniqueConstraint("id", "run_id", name="uq_tool_calls_id_run_id"),
        sa.UniqueConstraint(
            "run_id", "sequence_no", name="uq_tool_calls_run_id_sequence_no"
        ),
    )
    op.create_index(
        "ix_tool_calls_name_success", "tool_calls", ["tool_name", "success"]
    )
    op.create_index(
        "ix_tool_calls_run_started_at", "tool_calls", ["run_id", "started_at"]
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("raw_result_ref", sa.String(length=128), nullable=True),
        sa.Column(
            "attributes_payload", sa.JSON(), server_default="[]", nullable=False
        ),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_evidence_schema_version_positive"
        ),
        sa.CheckConstraint(
            "source_confidence >= 0 AND source_confidence <= 1",
            name="ck_evidence_source_confidence_valid",
        ),
        sa.CheckConstraint(
            "observed_at <= collected_at", name="ck_evidence_timeline_valid"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_evidence_run_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id", "run_id"],
            ["tool_calls.id", "tool_calls.run_id"],
            name="fk_evidence_tool_call_id_run_id_tool_calls",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
    )
    op.create_index(
        "ix_evidence_resource_observed_at",
        "evidence",
        ["resource", "observed_at"],
    )
    op.create_index("ix_evidence_run_source", "evidence", ["run_id", "source"])
    op.create_index("ix_evidence_tool_call_id", "evidence", ["tool_call_id"])

    op.create_table(
        "diagnosis_results",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("claims_payload", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("verification_payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_diagnosis_results_confidence_valid",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_diagnosis_results_schema_version_positive",
        ),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'PARTIAL')",
            name="ck_diagnosis_results_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["diagnosis_tasks.id"],
            name="fk_diagnosis_results_task_id_diagnosis_tasks",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["agent_runs.id", "agent_runs.task_id"],
            name="fk_diagnosis_results_run_id_task_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_diagnosis_results"),
        sa.UniqueConstraint("run_id", name="uq_diagnosis_results_run_id"),
    )
    op.create_index(
        "ix_diagnosis_results_task_created_at",
        "diagnosis_results",
        ["task_id", "created_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION opspilot_reject_evidence_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Evidence records are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_evidence_append_only
            BEFORE UPDATE OR DELETE ON evidence
            FOR EACH ROW EXECUTE FUNCTION opspilot_reject_evidence_mutation()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_evidence_append_only ON evidence")
        op.execute("DROP FUNCTION IF EXISTS opspilot_reject_evidence_mutation()")

    op.drop_index(
        "ix_diagnosis_results_task_created_at", table_name="diagnosis_results"
    )
    op.drop_table("diagnosis_results")
    op.drop_index("ix_evidence_tool_call_id", table_name="evidence")
    op.drop_index("ix_evidence_run_source", table_name="evidence")
    op.drop_index("ix_evidence_resource_observed_at", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_tool_calls_run_started_at", table_name="tool_calls")
    op.drop_index("ix_tool_calls_name_success", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_llm_calls_run_started_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_model", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index(
        "ix_prompt_versions_component_created_at", table_name="prompt_versions"
    )
    op.drop_table("prompt_versions")
    op.drop_index("ix_agent_runs_task_status", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index(
        "ix_diagnosis_tasks_status_created_at", table_name="diagnosis_tasks"
    )
    op.drop_table("diagnosis_tasks")

