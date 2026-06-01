"""Add `linkedin_dm` to the asset_type enum (W43).

Per-contact LinkedIn DM drafts surface as ContentAsset rows alongside
email + social_post. We never actually send via the LinkedIn API; the
draft is rendered for the SDR to copy/paste manually.

Postgres doesn't allow `ALTER TYPE ... DROP VALUE`, so the downgrade
recreates the enum from scratch — the migration is reversible only if
no row has been written with the new value yet (Alembic prints a
warning + fails in that case, which is what we want).

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE asset_type ADD VALUE IF NOT EXISTS 'linkedin_dm'")


def downgrade() -> None:
    # Re-create the enum without `linkedin_dm`. Fails (loudly, on purpose)
    # if any row uses the value.
    op.execute(
        """
        ALTER TYPE asset_type RENAME TO asset_type_old;
        CREATE TYPE asset_type AS ENUM (
            'email', 'social_post', 'ad_creative', 'blog_post',
            'landing_page_copy', 'sms', 'push'
        );
        ALTER TABLE content_asset ALTER COLUMN asset_type TYPE asset_type
            USING asset_type::text::asset_type;
        DROP TYPE asset_type_old;
        """
    )
