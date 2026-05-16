"""Add task.idempotency_key + leased_until + worker_id.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.add_column("task", sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task", sa.Column("worker_id", sa.String(length=100), nullable=True))

    # Per-tenant idempotency: same key may be reused across tenants.
    op.create_index(
        "idx_task_idempotency",
        "task",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    # Hot path for the reaper: find tasks whose lease has expired.
    op.create_index(
        "idx_task_leased_until",
        "task",
        ["leased_until"],
        postgresql_where=sa.text("status = 'running' AND leased_until IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_task_leased_until", table_name="task")
    op.drop_index("idx_task_idempotency", table_name="task")
    op.drop_column("task", "worker_id")
    op.drop_column("task", "leased_until")
    op.drop_column("task", "idempotency_key")
