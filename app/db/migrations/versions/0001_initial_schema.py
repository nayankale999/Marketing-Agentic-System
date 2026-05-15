"""Initial schema — snapshot of docs/backlog/schema.sql.

Revision ID: 0001
Revises:
Create Date: 2026-05-16
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_PATH = Path(__file__).parent / "0001_initial_schema.sql"


def upgrade() -> None:
    op.execute(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS personalisation_rule CASCADE;
        DROP TABLE IF EXISTS optimisation_recommendation CASCADE;
        DROP TABLE IF EXISTS suppression_entry CASCADE;
        DROP TABLE IF EXISTS integration_credential CASCADE;
        DROP TABLE IF EXISTS audit_log CASCADE;
        DROP TABLE IF EXISTS agent_log CASCADE;
        DROP TABLE IF EXISTS analytic_event CASCADE;
        DROP TABLE IF EXISTS ab_test CASCADE;
        DROP TABLE IF EXISTS approval_decision_log CASCADE;
        DROP TABLE IF EXISTS content_asset CASCADE;
        DROP TABLE IF EXISTS task CASCADE;
        DROP TABLE IF EXISTS audience_member CASCADE;
        DROP TABLE IF EXISTS audience CASCADE;
        DROP TABLE IF EXISTS campaign_channel_budget CASCADE;
        DROP TABLE IF EXISTS campaign CASCADE;
        DROP TABLE IF EXISTS channel CASCADE;
        DROP TABLE IF EXISTS agent CASCADE;
        DROP TABLE IF EXISTS app_user CASCADE;
        DROP TABLE IF EXISTS tenant CASCADE;
        DROP TYPE IF EXISTS event_kind CASCADE;
        DROP TYPE IF EXISTS approval_decision CASCADE;
        DROP TYPE IF EXISTS ab_test_status CASCADE;
        DROP TYPE IF EXISTS asset_status CASCADE;
        DROP TYPE IF EXISTS asset_type CASCADE;
        DROP TYPE IF EXISTS task_status CASCADE;
        DROP TYPE IF EXISTS channel_platform CASCADE;
        DROP TYPE IF EXISTS campaign_type CASCADE;
        DROP TYPE IF EXISTS campaign_status CASCADE;
        DROP TYPE IF EXISTS agent_status CASCADE;
        DROP TYPE IF EXISTS agent_kind CASCADE;
        DROP TYPE IF EXISTS user_role CASCADE;
        DROP FUNCTION IF EXISTS set_updated_at() CASCADE;
        """
    )
