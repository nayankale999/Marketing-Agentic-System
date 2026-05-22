"""compliance_rule table (W23, E06-S08).

Tenant-scoped per-rule suppression patterns the Content Creator checks every
draft against. Severity is the lever: `warn` triggers a rewrite-retry on hit,
`block` lets the draft land as `drafted` but flags it as compliance-blocked
so the campaign can't auto-advance to approval until a manager clears it.

Universal forbidden patterns (guarantees, medical claims, etc.) live in code
under app.agents._compliance — they apply to every tenant and don't need a
DB round-trip. This table is for tenant-specific compliance language.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compliance_rule",
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
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column(
            "pattern_kind",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'exact'"),
        ),
        sa.Column(
            "severity",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'warn'"),
        ),
        sa.Column("description", sa.Text(), nullable=True),
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
            "pattern_kind IN ('exact', 'regex')",
            name="ck_compliance_rule_pattern_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('warn', 'block')",
            name="ck_compliance_rule_severity",
        ),
        sa.UniqueConstraint(
            "tenant_id", "keyword", name="uq_compliance_rule_tenant_keyword"
        ),
    )
    op.create_index(
        "idx_compliance_rule_tenant_severity",
        "compliance_rule",
        ["tenant_id", "severity"],
    )

    op.execute("ALTER TABLE compliance_rule ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON compliance_rule "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON compliance_rule")
    op.drop_index(
        "idx_compliance_rule_tenant_severity", table_name="compliance_rule"
    )
    op.drop_table("compliance_rule")
