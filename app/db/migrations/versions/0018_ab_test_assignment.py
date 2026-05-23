"""ab_test_assignment table + ab_test config columns (W35, E09-S01/02).

E09-S02 needs a permanent record of which recipient saw which variant.
`ab_test_assignment` is keyed by `(tenant_id, ab_test_id,
audience_external_id)` so re-asking always returns the same answer.

E09-S01 adds traffic_split + min/max runtime + a partial unique index
that rejects a second active test on the same asset family.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ab_test",
        sa.Column(
            "traffic_split",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "ab_test",
        sa.Column("min_runtime_hours", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ab_test",
        sa.Column("max_runtime_hours", sa.Integer(), nullable=True),
    )

    # Reject a second active test on the same asset family. We key on
    # variant_a_id since that's the "primary" the family fans out from.
    op.create_index(
        "uq_ab_test_active_per_variant_a",
        "ab_test",
        ["tenant_id", "variant_a_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('designing', 'running') AND variant_a_id IS NOT NULL"
        ),
    )

    op.create_table(
        "ab_test_assignment",
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
            "ab_test_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ab_test.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # audience_member uses a composite PK, so this is the same
        # soft-reference pattern dispatch_attempt uses.
        sa.Column(
            "audience_external_id", sa.String(length=200), nullable=False
        ),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_asset.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "ab_test_id",
            "audience_external_id",
            name="uq_ab_test_assignment_tenant_test_audience",
        ),
    )

    op.execute("ALTER TABLE ab_test_assignment ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON ab_test_assignment "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON ab_test_assignment")
    op.drop_table("ab_test_assignment")
    op.drop_index("uq_ab_test_active_per_variant_a", table_name="ab_test")
    op.drop_column("ab_test", "max_runtime_hours")
    op.drop_column("ab_test", "min_runtime_hours")
    op.drop_column("ab_test", "traffic_split")
