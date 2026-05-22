"""strategy_proposal + tenant_constraint tables (W20, E05-S01/02/05).

Two related tables landing together:

  * `tenant_constraint` — admin-set guardrails the Strategist must respect.
    `kind = forbid_channel` is enforced in W20; `hard_cap` is accepted and
    stored now so the calendar work in W21 can use it without a second
    migration.

  * `strategy_proposal` — versioned plan history per campaign. `version`
    auto-increments in application code (MAX(version)+1 within campaign);
    a partial unique index keeps at most one accepted proposal per
    campaign — the accept endpoint clears any prior winner in the same
    transaction so the invariant holds without contention in normal flow.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_constraint",
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
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
            "kind IN ('forbid_channel', 'hard_cap')",
            name="ck_tenant_constraint_kind",
        ),
    )
    op.create_index(
        "idx_tenant_constraint_tenant_kind",
        "tenant_constraint",
        ["tenant_id", "kind"],
    )
    op.execute("ALTER TABLE tenant_constraint ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_constraint "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )

    op.create_table(
        "strategy_proposal",
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
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "is_accepted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_by_kind", sa.String(length=20), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "validation_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "campaign_id", "version", name="uq_strategy_proposal_campaign_version"
        ),
        sa.CheckConstraint(
            "created_by_kind IN ('user', 'agent', 'system')",
            name="ck_strategy_proposal_created_by_kind",
        ),
    )
    op.create_index(
        "idx_strategy_proposal_campaign_created",
        "strategy_proposal",
        ["campaign_id", sa.text("created_at DESC")],
    )
    # At most one accepted proposal per campaign.
    op.create_index(
        "idx_strategy_proposal_accepted",
        "strategy_proposal",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("is_accepted"),
    )
    op.execute("ALTER TABLE strategy_proposal ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON strategy_proposal "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON strategy_proposal")
    op.drop_index("idx_strategy_proposal_accepted", table_name="strategy_proposal")
    op.drop_index(
        "idx_strategy_proposal_campaign_created", table_name="strategy_proposal"
    )
    op.drop_table("strategy_proposal")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_constraint")
    op.drop_index("idx_tenant_constraint_tenant_kind", table_name="tenant_constraint")
    op.drop_table("tenant_constraint")
