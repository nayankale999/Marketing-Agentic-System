"""strategy_touchpoint table + updated_at on strategy_proposal (W21, E05-S03).

The calendar layer on top of an accepted strategy proposal. One row per
planned touch (channel × audience × scheduled_at). Tenant-isolated via RLS.

We add `updated_at` to `strategy_proposal` here rather than at 0010 because
the W20 design was append-only — each edit landed as a new version row. W21
introduces in-place mutability of a non-accepted proposal (touchpoint drags
bump the parent's `updated_at`), so the column finally earns its keep.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "strategy_proposal",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "strategy_touchpoint",
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
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_proposal.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_platform", sa.String(length=40), nullable=False),
        sa.Column(
            "audience_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audience.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "human_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "frequency_warning",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
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
    )
    op.create_index(
        "idx_strategy_touchpoint_proposal_time",
        "strategy_touchpoint",
        ["proposal_id", "scheduled_at"],
    )
    op.create_index(
        "idx_strategy_touchpoint_audience_time",
        "strategy_touchpoint",
        ["audience_id", "scheduled_at"],
    )

    op.execute("ALTER TABLE strategy_touchpoint ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON strategy_touchpoint "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON strategy_touchpoint")
    op.drop_index(
        "idx_strategy_touchpoint_audience_time", table_name="strategy_touchpoint"
    )
    op.drop_index(
        "idx_strategy_touchpoint_proposal_time", table_name="strategy_touchpoint"
    )
    op.drop_table("strategy_touchpoint")

    op.drop_column("strategy_proposal", "updated_at")
