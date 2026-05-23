"""ab_test significance + lift columns (W36, E09-S03).

Adds `last_evaluated_at` so the significance evaluator can honour the
"recompute at most every 15 minutes per test" cadence (E09-S03 AC #1)
without keeping that state in memory. `lift` persists the latest
treatment-vs-control delta so the dashboard can show it between
recomputes.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ab_test",
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ab_test",
        sa.Column("lift", sa.Numeric(8, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ab_test", "lift")
    op.drop_column("ab_test", "last_evaluated_at")
