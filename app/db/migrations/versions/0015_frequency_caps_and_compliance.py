"""frequency_cap_setting + tenant_compliance_settings + dispatch_attempt
status CHECK update (W29, E08-S04, E16-S04).

Three pieces:

  * frequency_cap_setting — per-tenant per-channel cap config. Default
    is `enabled=false` so existing tenants don't get a behavior change
    on upgrade; admins opt in per channel.

  * tenant_compliance_settings — per-tenant postal address (CAN-SPAM
    footer) + unsubscribe_secret (signs the public unsubscribe URL).
    Single row per tenant; rotating unsubscribe_secret invalidates
    every URL already in the wild — the documented revoke mechanism.

  * dispatch_attempt.status gains 'skipped' so the new frequency-cap
    branch can record a row per recipient without overloading
    'suppressed' (which means "on the suppression list", not "blocked
    by a rate limit").

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- frequency_cap_setting ------------------------------------------
    op.create_table(
        "frequency_cap_setting",
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
            "channel_platform",
            postgresql.ENUM(
                name="channel_platform", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "max_sends_per_recipient",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column(
            "window_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("7"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
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
        sa.CheckConstraint(
            "max_sends_per_recipient > 0",
            name="ck_frequency_cap_setting_max_positive",
        ),
        sa.CheckConstraint(
            "window_days > 0",
            name="ck_frequency_cap_setting_window_positive",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_platform",
            name="uq_frequency_cap_setting_tenant_channel",
        ),
    )
    op.execute("ALTER TABLE frequency_cap_setting ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON frequency_cap_setting "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )

    # ---- tenant_compliance_settings -------------------------------------
    op.create_table(
        "tenant_compliance_settings",
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
            unique=True,
        ),
        sa.Column("postal_address", sa.Text(), nullable=True),
        sa.Column(
            "unsubscribe_secret",
            sa.String(length=200),
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
    op.execute("ALTER TABLE tenant_compliance_settings ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_compliance_settings "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )

    # ---- dispatch_attempt.status: add 'skipped' --------------------------
    op.execute(
        "ALTER TABLE dispatch_attempt DROP CONSTRAINT ck_dispatch_attempt_status"
    )
    op.execute(
        "ALTER TABLE dispatch_attempt ADD CONSTRAINT ck_dispatch_attempt_status "
        "CHECK (status IN ('sent', 'suppressed', 'rejected', 'failed', 'skipped'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE dispatch_attempt DROP CONSTRAINT ck_dispatch_attempt_status"
    )
    op.execute(
        "ALTER TABLE dispatch_attempt ADD CONSTRAINT ck_dispatch_attempt_status "
        "CHECK (status IN ('sent', 'suppressed', 'rejected', 'failed'))"
    )

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_compliance_settings")
    op.drop_table("tenant_compliance_settings")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON frequency_cap_setting")
    op.drop_table("frequency_cap_setting")
