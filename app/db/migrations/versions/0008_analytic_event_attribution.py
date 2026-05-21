"""analytic_event: nullable campaign_id + provider_event_id for dedup.

E01-S04 requires unattributed events (no UTM match) to be stored with
campaign_id = NULL. The original schema typed it NOT NULL — we relax it
here. We also add `provider_event_id` so the connector can dedup against
prior fetches using `ON CONFLICT DO NOTHING`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "analytic_event",
        "campaign_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "analytic_event",
        sa.Column("provider_event_id", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "idx_analytic_event_provider_event_id",
        "analytic_event",
        ["tenant_id", "provider_event_id"],
        unique=True,
        postgresql_where=sa.text("provider_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_analytic_event_provider_event_id", table_name="analytic_event"
    )
    op.drop_column("analytic_event", "provider_event_id")
    op.alter_column(
        "analytic_event",
        "campaign_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
