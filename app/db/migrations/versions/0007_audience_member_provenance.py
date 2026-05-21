"""audience_member: source + fetched_at provenance columns.

E01-S05 requires every ingested record to carry its source and the
timestamp it was sourced at. Existing rows pre-W13 don't have these fields
— we leave them NULL rather than backfilling with a guess.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audience_member",
        sa.Column("source", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "audience_member",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Hot path for the freshness query: count members per audience whose
    # fetched_at is older than the TTL window.
    op.create_index(
        "idx_audience_member_fetched_at",
        "audience_member",
        ["audience_id", "fetched_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_audience_member_fetched_at", table_name="audience_member")
    op.drop_column("audience_member", "fetched_at")
    op.drop_column("audience_member", "source")
