"""custom_kpi + spend_reconciliation tables (W41, E10-S07 / E10-S06).

Both tables follow the standard RLS + tenant_id pattern. `custom_kpi.
campaign_id` is nullable so a KPI can be tenant-wide or campaign-scoped.
`custom_kpi.deleted_at` enables soft delete (E10-S07 AC #4: deletion
preserves historical numbers, the KPI just gets greyed out).

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- custom_kpi ----
    op.create_table(
        "custom_kpi",
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
            nullable=True,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "formula",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_custom_kpi_tenant_campaign",
        "custom_kpi",
        ["tenant_id", "campaign_id"],
    )
    op.execute("ALTER TABLE custom_kpi ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON custom_kpi "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )

    # ---- spend_reconciliation ----
    op.create_table(
        "spend_reconciliation",
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
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("committed_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("invoiced_amount", sa.Numeric(14, 2), nullable=False),
        # Signed delta: (invoiced - committed) / committed * 100. Stored as
        # NUMERIC so a 0.5% difference is faithfully represented.
        sa.Column("delta_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'matched', 'explained', 'disputed')",
            name="ck_spend_reconciliation_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "period_start",
            "period_end",
            name="uq_spend_reconciliation_period",
        ),
    )
    op.execute("ALTER TABLE spend_reconciliation ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON spend_reconciliation "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON spend_reconciliation")
    op.drop_table("spend_reconciliation")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON custom_kpi")
    op.drop_index("idx_custom_kpi_tenant_campaign", table_name="custom_kpi")
    op.drop_table("custom_kpi")
