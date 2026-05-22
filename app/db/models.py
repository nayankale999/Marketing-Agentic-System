"""SQLAlchemy ORM for the seven Slice-1 tables.

Schema-of-record is `migrations/versions/0001_initial_schema.sql`. The remaining
tables (channel, audience, content_asset, ab_test, etc.) get ORM coverage as
later work units need them.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.dialects.postgresql import ENUM as PgEnum  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import (
    AgentKind,
    AgentStatus,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    EventKind,
    TaskStatus,
    UserRole,
)

# Postgres ENUMs are created by the SQL migration. We reference them with
# `create_type=False` so SQLAlchemy never tries to (re)create them.
_USER_ROLE = PgEnum(
    UserRole,
    name="user_role",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_AGENT_KIND = PgEnum(
    AgentKind,
    name="agent_kind",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_AGENT_STATUS = PgEnum(
    AgentStatus,
    name="agent_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_CAMPAIGN_STATUS = PgEnum(
    CampaignStatus,
    name="campaign_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_CAMPAIGN_TYPE = PgEnum(
    CampaignType,
    name="campaign_type",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_TASK_STATUS = PgEnum(
    TaskStatus,
    name="task_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_CHANNEL_PLATFORM = PgEnum(
    ChannelPlatform,
    name="channel_platform",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_EVENT_KIND = PgEnum(
    EventKind,
    name="event_kind",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    oidc_hosted_domain: Mapped[str | None] = mapped_column(CITEXT)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, server_default="standard")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(_USER_ROLE, nullable=False, server_default="marketer")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Agent(Base):
    __tablename__ = "agent"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_type: Mapped[AgentKind] = mapped_column(_AGENT_KIND, nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        _AGENT_STATUS, nullable=False, server_default="idle"
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Campaign(Base):
    __tablename__ = "campaign"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        _CAMPAIGN_STATUS, nullable=False, server_default="drafted"
    )
    campaign_type: Mapped[CampaignType] = mapped_column(_CAMPAIGN_TYPE, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    budget_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default="0"
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="USD")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    brief: Mapped[str | None] = mapped_column(Text)
    kpi_targets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Task(Base):
    __tablename__ = "task"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign.id", ondelete="CASCADE")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.id", ondelete="RESTRICT"), nullable=False
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task.id", ondelete="SET NULL")
    )
    skill_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        _TASK_STATUS, nullable=False, server_default="queued"
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="5")
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="3")
    # Per-tenant idempotency key; partial unique index in migration 0005.
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    # Set on claim; the reaper resets these to NULL on expiry.
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(100))
    input_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    output_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error_message: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (CheckConstraint("actor_kind IN ('user', 'agent', 'system')"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entity_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # `metadata` is reserved on DeclarativeBase; map a renamed attribute to the real column.
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentLog(Base):
    __tablename__ = "agent_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    log_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    severity: Mapped[str] = mapped_column(String(20), nullable=False, server_default="info")
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Channel(Base):
    """Publishing channel (email/social/ads) attached to a tenant.

    Minimal stub registered so foreign keys from `integration_credential.channel_id`
    resolve at ORM-load time. Full coverage (CRUD endpoints, agent integration)
    lands in Slice 4.
    """

    __tablename__ = "channel"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[ChannelPlatform] = mapped_column(_CHANNEL_PLATFORM, nullable=False)
    api_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IntegrationCredential(Base):
    """OAuth tokens / API keys for an external provider (CRM, email, social).

    The encrypted_payload is Fernet-encrypted JSON via
    `app.integrations.credentials.EncryptedPayload`. provider is a free-form
    string (e.g. 'hubspot', 'salesforce', 'dynamics365'). channel_id is set
    when the credential is for a publishing channel (email/social/ads);
    NULL for CRM and analytics providers.
    """

    __tablename__ = "integration_credential"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channel.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False, server_default="default")
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Audience(Base):
    """A targeted set of contacts attached to a campaign.

    `segment_criteria` carries the rule set for ICP-driven audiences (Slice 2
    work units E04 onward); for CSV-uploaded "seed" audiences (E01-S02) we
    record `{"source": "csv", "filename": "...", "uploaded_at": "..."}` and
    the members live in `audience_member`.
    """

    __tablename__ = "audience"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    segment_criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    estimated_size: Mapped[int | None] = mapped_column(Integer)
    actual_size: Mapped[int | None] = mapped_column(Integer)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AudienceMember(Base):
    """One contact materialised into an audience. Composite PK
    `(audience_id, external_id)` — external_id is normally the lowercased
    email for CSV uploads, CRM contact id for synced sources.

    `source` + `fetched_at` provenance (E01-S05) are nullable to accommodate
    rows that predate migration 0007; all new inserts set both.
    """

    __tablename__ = "audience_member"

    audience_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("audience.id", ondelete="CASCADE"),
        primary_key=True,
    )
    external_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    source: Mapped[str | None] = mapped_column(String(20))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnalyticEvent(Base):
    """One metric snapshot from a web analytics / ads / email provider.

    `provider_event_id` (added in migration 0008) is the dedup key — a
    composite the connector builds out of (provider, scope, period, dim).
    `campaign_id` is nullable for unattributed events (UTM that doesn't
    match any campaign). The raw provider payload (utm_*, country, etc.)
    lands in `payload` so we can re-attribute later if rules change.
    """

    __tablename__ = "analytic_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign.id", ondelete="RESTRICT")
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channel.id", ondelete="SET NULL")
    )
    event_type: Mapped[EventKind] = mapped_column(_EVENT_KIND, nullable=False)
    metric_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    provider_event_id: Mapped[str | None] = mapped_column(String(200))
    event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BrandVoice(Base):
    """Tenant voice configuration (W19, E06-S02)."""

    __tablename__ = "brand_voice"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_brand_voice_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    tone_descriptors: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    do_words: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    dont_words: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    sample_paragraphs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    reading_grade_target: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
