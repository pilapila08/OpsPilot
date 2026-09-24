"""Add V2 planning round audit without changing V1 rows.

Revision ID: 20260923_0004
Revises: 20260923_0003
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260923_0004"
down_revision: str | None = "20260923_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planning_rounds",
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("evidence_ids_payload", sa.JSON(), nullable=False),
        sa.Column("call_ids_payload", sa.JSON(), nullable=False),
        sa.Column("validation_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("round_no BETWEEN 1 AND 4", name="ck_planning_rounds_round_no_valid"),
        sa.CheckConstraint("action IN ('continue', 'finish', 'partial')", name="ck_planning_rounds_action_valid"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_planning_rounds_run_id_agent_runs", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"], name="fk_planning_rounds_prompt_version_id_prompt_versions", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("run_id", "round_no", name="pk_planning_rounds"),
    )
    op.create_index("ix_planning_rounds_run_round", "planning_rounds", ["run_id", "round_no"])


def downgrade() -> None:
    op.drop_index("ix_planning_rounds_run_round", table_name="planning_rounds")
    op.drop_table("planning_rounds")
