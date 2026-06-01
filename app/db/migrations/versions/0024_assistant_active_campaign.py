"""assistant_conversation.active_campaign_id (W42.3).

Persistent "what campaign were we working on" pointer that survives the
message-window trim. Without this, a long create→audience→strategy→
content→approve flow blows past MAX_MESSAGES and the assistant loses
context about what the user means by "approve it" / "launch it now."

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversation",
        sa.Column(
            "active_campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaign.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("assistant_conversation", "active_campaign_id")
