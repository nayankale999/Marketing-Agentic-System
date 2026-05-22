"""provider_rate_limit table (W31, E08-S06).

Per-tenant per-provider rate limit config. The schema lands now so the
admin surface + the partial enforcement (429-Retry-After honoring) work
end-to-end. Full token-bucket pacing during dispatch is deferred to a
polish unit — this table is the seam where that work will plug in.

Default `enabled=false` so the limit isn't enforced until an admin opts
in per provider.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_rate_limit",
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
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "requests_per_minute",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
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
            "requests_per_minute > 0",
            name="ck_provider_rate_limit_rpm_positive",
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider", name="uq_provider_rate_limit_tenant_provider"
        ),
    )
    op.execute("ALTER TABLE provider_rate_limit ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON provider_rate_limit "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON provider_rate_limit")
    op.drop_table("provider_rate_limit")
