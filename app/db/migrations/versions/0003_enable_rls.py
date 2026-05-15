"""Enable Row-Level Security on every domain table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-16
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_PATH = Path(__file__).parent / "0003_enable_rls.sql"

_TABLES = [
    "tenant",
    "app_user",
    "agent",
    "channel",
    "campaign",
    "campaign_channel_budget",
    "audience",
    "audience_member",
    "task",
    "content_asset",
    "approval_decision_log",
    "ab_test",
    "analytic_event",
    "agent_log",
    "audit_log",
    "integration_credential",
    "suppression_entry",
    "optimisation_recommendation",
    "personalisation_rule",
]


def upgrade() -> None:
    op.execute(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    drops = "\n".join(
        f"DROP POLICY IF EXISTS tenant_isolation ON {t};\n"
        f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;"
        for t in _TABLES
    )
    op.execute(drops)
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM mas_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE USAGE, SELECT ON SEQUENCES FROM mas_app;
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mas_app;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM mas_app;
        REVOKE USAGE ON SCHEMA public FROM mas_app;
        REVOKE mas_app FROM CURRENT_USER;
        DROP ROLE IF EXISTS mas_app;
        """
    )
