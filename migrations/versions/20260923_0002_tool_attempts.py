"""Track logical Tool calls and their distinct retry attempts.

Revision ID: 20260923_0002
Revises: 20260922_0001
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260923_0002"
down_revision: str | None = "20260922_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("logical_call_id", sa.String(128), nullable=True))
    op.add_column("tool_calls", sa.Column("attempt_no", sa.Integer(), nullable=True))
    op.execute("UPDATE tool_calls SET logical_call_id = id, attempt_no = 1")
    with op.batch_alter_table("tool_calls") as batch:
        batch.alter_column("logical_call_id", existing_type=sa.String(128), nullable=False)
        batch.alter_column("attempt_no", existing_type=sa.Integer(), nullable=False)
        batch.create_check_constraint("ck_tool_calls_attempt_no_positive", "attempt_no >= 1")
        batch.create_unique_constraint(
            "uq_tool_calls_run_id_logical_call_id_attempt_no",
            ["run_id", "logical_call_id", "attempt_no"],
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_calls") as batch:
        batch.drop_constraint("uq_tool_calls_run_id_logical_call_id_attempt_no", type_="unique")
        batch.drop_constraint("ck_tool_calls_attempt_no_positive", type_="check")
        batch.drop_column("attempt_no")
        batch.drop_column("logical_call_id")
