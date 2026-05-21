"""integration_credential.provider -> VARCHAR + partial unique index.

The schema originally typed `provider` as `channel_platform` (email/linkedin/
etc.), but CRM connectors (HubSpot, Salesforce, Dynamics) aren't publishing
channels and don't belong in that enum. Widening to VARCHAR lets a single
table back every integration kind. The partial unique index keeps "one cred
per (tenant, provider, label)" when no channel is attached (the common case
for CRMs).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE integration_credential "
        "ALTER COLUMN provider TYPE VARCHAR(64) USING provider::text"
    )
    op.create_index(
        "idx_integration_credential_provider_label",
        "integration_credential",
        ["tenant_id", "provider", "label"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_integration_credential_provider_label",
        table_name="integration_credential",
    )
    op.execute(
        "ALTER TABLE integration_credential "
        "ALTER COLUMN provider TYPE channel_platform USING provider::channel_platform"
    )
