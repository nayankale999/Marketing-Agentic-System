"""metric_anomaly + optimisation_recommendation tables (W37, E10-S02/03).

Both tables follow the established RLS + tenant_id pattern. The
`auto_pause_on_critical_anomaly` flag rides on tenant_compliance_settings
because that's where opt-in safety nets already live.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- metric_anomaly ----
    op.create_table(
        "metric_anomaly",
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
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaign.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_value", sa.Numeric(18, 6), nullable=False),
        sa.Column("baseline_median", sa.Numeric(18, 6), nullable=False),
        sa.Column("baseline_stddev", sa.Numeric(18, 6), nullable=False),
        sa.Column("sigma", sa.Numeric(8, 4), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "dismissed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "severity IN ('warning', 'critical')",
            name="ck_metric_anomaly_severity",
        ),
    )
    op.create_index(
        "idx_metric_anomaly_campaign_metric_created",
        "metric_anomaly",
        ["campaign_id", "metric", sa.text("created_at DESC")],
    )
    op.execute("ALTER TABLE metric_anomaly ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON metric_anomaly "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )

    # ---- optimisation_recommendation ----
    # The Phase-2 sketch in schema.sql created this table at migration 0001,
    # and 0003 enabled RLS on it. W37 promotes it to MVP — add the missing
    # `rationale` / `predicted_uplift` columns + the `kind` CHECK
    # constraint that the MVP needs.
    op.add_column(
        "optimisation_recommendation",
        sa.Column("rationale", sa.Text(), nullable=True),
    )
    op.add_column(
        "optimisation_recommendation",
        sa.Column("predicted_uplift", sa.Numeric(6, 4), nullable=True),
    )
    op.create_check_constraint(
        "ck_optimisation_recommendation_kind",
        "optimisation_recommendation",
        "kind IN ('budget_shift', 'creative_swap', 'schedule_change')",
    )

    # ---- tenant_compliance_settings.auto_pause_on_critical_anomaly ----
    op.add_column(
        "tenant_compliance_settings",
        sa.Column(
            "auto_pause_on_critical_anomaly",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "tenant_compliance_settings", "auto_pause_on_critical_anomaly"
    )
    op.drop_constraint(
        "ck_optimisation_recommendation_kind",
        "optimisation_recommendation",
        type_="check",
    )
    op.drop_column("optimisation_recommendation", "predicted_uplift")
    op.drop_column("optimisation_recommendation", "rationale")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON metric_anomaly")
    op.drop_index(
        "idx_metric_anomaly_campaign_metric_created", table_name="metric_anomaly"
    )
    op.drop_table("metric_anomaly")
