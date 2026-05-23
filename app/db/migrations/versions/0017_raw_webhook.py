"""raw_webhook table (W33, E12-S06).

Every inbound webhook lands here — signature-verified or not — for audit +
the "unmapped events" admin view. Rows with `mapped_event_id IS NULL` are
the unmapped ones; the partial index makes that lookup fast.

`tenant_id` is nullable because we accept the row even when the URL's
tenant_id doesn't match a tenant (e.g., probes / misrouted callbacks).
RLS still applies — tenant sessions only see their own rows; cross-tenant
debugging happens via admin sessions that bypass RLS.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_webhook",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("signature_reason", sa.Text(), nullable=True),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("payload", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "mapped_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analytic_event.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_raw_webhook_provider_time",
        "raw_webhook",
        ["provider", sa.text("received_at DESC")],
    )
    # Partial index — the "unmapped events" hot path.
    op.create_index(
        "idx_raw_webhook_unmapped",
        "raw_webhook",
        ["received_at"],
        postgresql_where=sa.text("mapped_event_id IS NULL"),
    )
    op.execute("ALTER TABLE raw_webhook ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON raw_webhook "
        "USING ("
        "tenant_id IS NULL "
        "OR tenant_id = current_setting('app.tenant_id', true)::uuid"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON raw_webhook")
    op.drop_index("idx_raw_webhook_unmapped", table_name="raw_webhook")
    op.drop_index("idx_raw_webhook_provider_time", table_name="raw_webhook")
    op.drop_table("raw_webhook")
