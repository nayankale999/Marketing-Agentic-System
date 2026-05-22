"""dispatch_attempt table (W28, E08-S02 + E08-S05).

One row per (content_asset × recipient) attempt by the Distribution agent.
The unique `(tenant_id, idempotency_key)` constraint is the load-bearing piece
of E08-S05 #1: a retry of the same dispatch task can't double-send because
the upsert hits the conflict and the prior row's status tells us whether the
provider already accepted it.

`idempotency_key` shape: `asset:{content_asset_id}:recipient:{lowercase_email}`.
Deterministic from (asset, recipient) so retries reuse it without coordination.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dispatch_attempt",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "content_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_asset.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # audience_member has a composite PK (audience_id, external_id) —
        # no `id` column to FK against. We correlate by `recipient_identifier`
        # instead and keep `audience_external_id` as a soft reference for
        # debugging.
        sa.Column(
            "audience_external_id",
            sa.String(length=200),
            nullable=True,
        ),
        sa.Column("recipient_identifier", postgresql.CITEXT(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('sent', 'suppressed', 'rejected', 'failed')",
            name="ck_dispatch_attempt_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_dispatch_attempt_tenant_idem_key",
        ),
    )
    op.create_index(
        "idx_dispatch_attempt_asset_status",
        "dispatch_attempt",
        ["content_asset_id", "status"],
    )
    # Hot path for E08-S05 #3: "any sent attempt to this recipient in the
    # last 24h" → the secondary dedup window when the provider id wasn't
    # captured at send time.
    op.create_index(
        "idx_dispatch_attempt_recipient_recent",
        "dispatch_attempt",
        ["tenant_id", "recipient_identifier", "sent_at"],
    )
    op.execute("ALTER TABLE dispatch_attempt ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON dispatch_attempt "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON dispatch_attempt")
    op.drop_index(
        "idx_dispatch_attempt_recipient_recent", table_name="dispatch_attempt"
    )
    op.drop_index("idx_dispatch_attempt_asset_status", table_name="dispatch_attempt")
    op.drop_table("dispatch_attempt")
