"""Persist the API request mode and case for idempotent task creation.

Revision ID: 20260923_0003
Revises: 20260923_0002
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260923_0003"
down_revision: str | None = "20260923_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("diagnosis_tasks", sa.Column("mode", sa.String(16), nullable=True))
    op.add_column("diagnosis_tasks", sa.Column("case_id", sa.String(128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("diagnosis_tasks") as batch:
        batch.drop_column("case_id")
        batch.drop_column("mode")
