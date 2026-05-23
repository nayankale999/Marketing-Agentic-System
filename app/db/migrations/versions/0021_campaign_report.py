"""campaign_report table (W38, E10-S04 / E13-S04).

End-of-campaign performance reports are append-only snapshots: every
regeneration writes a new row with `version = N+1` and flips the prior
latest row's `is_latest=false`. The partial unique index enforces
exactly-one-latest per campaign so the dashboard's default view is
unambiguous.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_report",
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
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaign.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Free-form actor label so we can record "system" for the
        # auto-generate hook + a UUID for manual regenerations.
        sa.Column("generated_by", sa.String(length=100), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_latest",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "version",
            name="uq_campaign_report_tenant_campaign_version",
        ),
    )
    # Exactly-one-latest per campaign — enforced at the DB level so
    # concurrent regenerations can't break the invariant.
    op.create_index(
        "uq_campaign_report_latest",
        "campaign_report",
        ["tenant_id", "campaign_id"],
        unique=True,
        postgresql_where=sa.text("is_latest = true"),
    )
    op.execute("ALTER TABLE campaign_report ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON campaign_report "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON campaign_report")
    op.drop_index("uq_campaign_report_latest", table_name="campaign_report")
    op.drop_table("campaign_report")
