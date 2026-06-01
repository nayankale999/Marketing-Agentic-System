"""Python enums mirroring the PostgreSQL enums declared in `0001_initial_schema.sql`.

Values must match the SQL exactly (string-compared by Postgres).
"""

import enum


class UserRole(enum.StrEnum):
    admin = "admin"
    manager = "manager"
    marketer = "marketer"
    viewer = "viewer"


class AgentKind(enum.StrEnum):
    orchestrator = "orchestrator"
    campaign_strategist = "campaign_strategist"
    audience_targeting = "audience_targeting"
    content_creator = "content_creator"
    approval_orchestrator = "approval_orchestrator"
    channel_distribution = "channel_distribution"
    analytics_optimisation = "analytics_optimisation"


class AgentStatus(enum.StrEnum):
    idle = "idle"
    busy = "busy"
    degraded = "degraded"
    disabled = "disabled"


class CampaignStatus(enum.StrEnum):
    drafted = "drafted"
    audience_built = "audience_built"
    strategy_set = "strategy_set"
    content_in_production = "content_in_production"
    approval_pending = "approval_pending"
    ready_to_launch = "ready_to_launch"
    live = "live"
    optimising = "optimising"
    paused = "paused"
    completed = "completed"


class CampaignType(enum.StrEnum):
    awareness = "awareness"
    lead_gen = "lead_gen"
    demand_gen = "demand_gen"
    nurture = "nurture"
    product_launch = "product_launch"
    event_promo = "event_promo"
    retention = "retention"
    reactivation = "reactivation"


class ChannelPlatform(enum.StrEnum):
    email = "email"
    linkedin = "linkedin"
    x = "x"
    meta = "meta"
    instagram = "instagram"
    google_ads = "google_ads"
    meta_ads = "meta_ads"
    web = "web"
    blog = "blog"
    sms = "sms"


class TaskStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    awaiting_retry = "awaiting_retry"


class AssetType(enum.StrEnum):
    email = "email"
    social_post = "social_post"
    ad_creative = "ad_creative"
    blog_post = "blog_post"
    landing_page_copy = "landing_page_copy"
    sms = "sms"
    push = "push"
    # W43 — per-contact LinkedIn DM drafts. No real send: we generate the
    # text and surface it for the SDR to copy/paste manually.
    linkedin_dm = "linkedin_dm"


class AssetStatus(enum.StrEnum):
    requested = "requested"
    generating = "generating"
    drafted = "drafted"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    scheduled = "scheduled"
    published = "published"
    failed = "failed"
    measuring = "measuring"
    variant_winner = "variant_winner"
    archived = "archived"


class AbTestStatus(enum.StrEnum):
    designing = "designing"
    running = "running"
    significant = "significant"
    inconclusive = "inconclusive"
    stopped = "stopped"


class ApprovalDecision(enum.StrEnum):
    approved = "approved"
    approved_with_edits = "approved_with_edits"
    rejected = "rejected"


class EventKind(enum.StrEnum):
    impression = "impression"
    click = "click"
    open = "open"
    reply = "reply"
    conversion = "conversion"
    unsubscribe = "unsubscribe"
    bounce = "bounce"
    spam_complaint = "spam_complaint"
    spend = "spend"


class TenantConstraintKind(enum.StrEnum):
    """Admin-set guardrails the Strategist must respect (W20, E05-S05).

    W20 enforces `forbid_channel`; `hard_cap` is accepted and stored but its
    enforcement lands in W21 once the sequence calendar exists.
    """

    forbid_channel = "forbid_channel"
    hard_cap = "hard_cap"


class CompliancePatternKind(enum.StrEnum):
    """How `compliance_rule.keyword` should be matched (W23, E06-S08)."""

    exact = "exact"
    regex = "regex"


class ComplianceSeverity(enum.StrEnum):
    """How a compliance hit affects the draft (W23, E06-S08).

    `warn` triggers a rewrite-retry to avoid the term; `block` lets the draft
    land as `drafted` but prevents auto-promotion until a manager clears it.
    """

    warn = "warn"
    block = "block"


class ApprovalRejectionCategory(enum.StrEnum):
    """Common buckets a reviewer assigns alongside the free-text reason on a
    reject (W25, E07-S02 #3). Used as a hint to the regenerate prompt and
    later as a clustering signal for E07-S05 rejection-pattern analytics."""

    off_voice = "off_voice"
    inaccurate = "inaccurate"
    wrong_audience = "wrong_audience"
    length = "length"
    compliance = "compliance"
    other = "other"
