"""Revoke UPDATE/DELETE on audit_log + agent_log from mas_app (append-only).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        REVOKE UPDATE, DELETE ON audit_log FROM mas_app;
        REVOKE UPDATE, DELETE ON agent_log FROM mas_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        GRANT UPDATE, DELETE ON audit_log TO mas_app;
        GRANT UPDATE, DELETE ON agent_log TO mas_app;
        """
    )
