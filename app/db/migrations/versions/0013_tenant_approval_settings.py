"""tenant_approval_settings table (W26, E07-S03/04).

One row per tenant. Holds the spend thresholds that drive approval gating:

  * `admin_required_above_amount` — campaign.budget_total over this requires
    `admin` role on approve. Nullable = no such requirement.
  * `auto_approval_cap_amount` — batch-approve excludes any asset whose
    campaign.budget_total exceeds this. Default 0 means batch never auto-
    approves until the admin explicitly raises the cap (the AC's literal
    'default zero' wording).

`currency` lets thresholds and campaign budgets be compared apples-to-apples;
mismatched currencies skip the threshold rather than convert (no FX plumbing
in MVP).

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_approval_settings",
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
            unique=True,
        ),
        sa.Column(
            "admin_required_above_amount",
            sa.Numeric(14, 2),
            nullable=True,
        ),
        sa.Column(
            "auto_approval_cap_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "currency",
            sa.CHAR(3),
            nullable=False,
            server_default=sa.text("'USD'"),
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
            "admin_required_above_amount IS NULL OR admin_required_above_amount >= 0",
            name="ck_tenant_approval_settings_admin_amount_nonneg",
        ),
        sa.CheckConstraint(
            "auto_approval_cap_amount >= 0",
            name="ck_tenant_approval_settings_cap_nonneg",
        ),
    )
    op.execute("ALTER TABLE tenant_approval_settings ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_approval_settings "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_approval_settings")
    op.drop_table("tenant_approval_settings")
