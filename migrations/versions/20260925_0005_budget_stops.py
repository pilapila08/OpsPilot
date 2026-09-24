"""Add one auditable first budget stop per Run.

Revision ID: 20260925_0005
Revises: 20260923_0004
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260925_0005"
down_revision: str | None = "20260923_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budget_stops",
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("dimension", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(128), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("requested", sa.Numeric(30, 18), nullable=False),
        sa.Column("budget_payload", sa.JSON(), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('steps', 'tool_calls', 'retries', 'tokens', 'cost', 'elapsed')",
            name="ck_budget_stops_dimension_valid",
        ),
        sa.CheckConstraint("length(phase) > 0", name="ck_budget_stops_phase_nonempty"),
        sa.CheckConstraint("step_no >= 1", name="ck_budget_stops_step_no_positive"),
        sa.CheckConstraint(
            "kind IN ('exhausted', 'projected', 'deadline')",
            name="ck_budget_stops_kind_valid",
        ),
        sa.CheckConstraint("requested >= 0", name="ck_budget_stops_requested_nonnegative"),
        sa.CheckConstraint(
            "round_no IS NULL OR round_no BETWEEN 1 AND 4",
            name="ck_budget_stops_round_no_valid",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"],
            name="fk_budget_stops_run_id_agent_runs", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_budget_stops"),
    )


def downgrade() -> None:
    op.drop_table("budget_stops")
