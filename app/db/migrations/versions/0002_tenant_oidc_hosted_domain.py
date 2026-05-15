"""Add tenant.oidc_hosted_domain.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column("oidc_hosted_domain", postgresql.CITEXT(), nullable=True),
    )
    op.create_index(
        "idx_tenant_oidc_hosted_domain",
        "tenant",
        ["oidc_hosted_domain"],
        unique=True,
        postgresql_where=sa.text("oidc_hosted_domain IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_tenant_oidc_hosted_domain", table_name="tenant")
    op.drop_column("tenant", "oidc_hosted_domain")
