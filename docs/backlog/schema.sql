-- =============================================================================
-- MAS — Marketing Agentic System
-- PostgreSQL schema (MVP + Phase 2 experiment tables)
--
-- Design principles
--   - UUID primary keys via pgcrypto
--   - Multi-tenant from day one (tenant_id on every domain table, enforced by FK)
--   - created_at / updated_at on every mutable row, trigger-driven
--   - Append-only audit_log + agent_logs (revoke UPDATE/DELETE from app role on deploy)
--   - ON DELETE chosen per relationship:
--       CASCADE   for owned children (campaign -> task, campaign -> content_asset)
--       RESTRICT  for audit-sensitive links (analytic_event, agent_log, audit_log)
--       SET NULL  for optional owner references (campaign.owner_id)
--   - Money stored as NUMERIC(14,2) with explicit currency code
--   - JSONB everywhere for config / payloads, with documented top-level keys per table
--
-- Tested against: PostgreSQL 15+.
-- Run:  createdb mas_check && psql mas_check -f schema.sql
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive emails / domains
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy search on names/titles

-- -----------------------------------------------------------------------------
-- Shared helpers
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- ENUMS
-- =============================================================================

CREATE TYPE user_role AS ENUM (
    'admin',          -- tenant-wide configuration, integrations, RBAC
    'manager',        -- approve, override, view all campaigns
    'marketer',       -- create + edit own campaigns, submit for approval
    'viewer'          -- read-only
);

CREATE TYPE agent_kind AS ENUM (
    'orchestrator',
    'campaign_strategist',
    'audience_targeting',
    'content_creator',
    'approval_orchestrator',
    'channel_distribution',
    'analytics_optimisation'
);

CREATE TYPE agent_status AS ENUM (
    'idle',
    'busy',
    'degraded',
    'disabled'
);

CREATE TYPE campaign_status AS ENUM (
    'drafted',
    'audience_built',
    'strategy_set',
    'content_in_production',
    'approval_pending',
    'ready_to_launch',
    'live',
    'optimising',
    'paused',
    'completed'
);

CREATE TYPE campaign_type AS ENUM (
    'awareness',
    'lead_gen',
    'demand_gen',
    'nurture',
    'product_launch',
    'event_promo',
    'retention',
    'reactivation'
);

CREATE TYPE channel_platform AS ENUM (
    'email',
    'linkedin',
    'x',
    'meta',
    'instagram',
    'google_ads',
    'meta_ads',
    'web',
    'blog',
    'sms'
);

CREATE TYPE task_status AS ENUM (
    'queued',
    'running',
    'succeeded',
    'failed',
    'cancelled',
    'awaiting_retry'
);

CREATE TYPE asset_type AS ENUM (
    'email',
    'social_post',
    'ad_creative',
    'blog_post',
    'landing_page_copy',
    'sms',
    'push'
);

CREATE TYPE asset_status AS ENUM (
    'requested',
    'generating',
    'drafted',
    'pending_approval',
    'approved',
    'rejected',
    'scheduled',
    'published',
    'failed',
    'measuring',
    'variant_winner',
    'archived'
);

CREATE TYPE ab_test_status AS ENUM (
    'designing',
    'running',
    'significant',
    'inconclusive',
    'stopped'
);

CREATE TYPE approval_decision AS ENUM (
    'approved',
    'approved_with_edits',
    'rejected'
);

CREATE TYPE event_kind AS ENUM (
    'impression',
    'click',
    'open',
    'reply',
    'conversion',
    'unsubscribe',
    'bounce',
    'spam_complaint',
    'spend'
);

-- =============================================================================
-- TENANCY, IDENTITY, RBAC
-- =============================================================================

CREATE TABLE tenant (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(200) NOT NULL,
    domain              CITEXT      UNIQUE,
    oidc_hosted_domain  CITEXT,     -- matched against the OIDC `hd` claim
    plan                VARCHAR(50) NOT NULL DEFAULT 'standard',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_tenant_oidc_hosted_domain
    ON tenant (oidc_hosted_domain)
    WHERE oidc_hosted_domain IS NOT NULL;

CREATE TRIGGER tenant_set_updated_at BEFORE UPDATE ON tenant
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE app_user (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    email           CITEXT      NOT NULL,
    display_name    VARCHAR(200),
    role            user_role   NOT NULL DEFAULT 'marketer',
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);

CREATE TRIGGER app_user_set_updated_at BEFORE UPDATE ON app_user
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_app_user_tenant ON app_user(tenant_id) WHERE is_active;

-- =============================================================================
-- AGENT REGISTRY  (matches `agents` table in DBSchema.png)
-- =============================================================================

CREATE TABLE agent (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    agent_type      agent_kind  NOT NULL,
    status          agent_status NOT NULL DEFAULT 'idle',
    -- config JSONB top-level keys: { model, system_prompt, max_tokens, tool_allowlist[], rate_limit }
    config          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    last_active_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, name)
);

CREATE TRIGGER agent_set_updated_at BEFORE UPDATE ON agent
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_agent_tenant_type ON agent(tenant_id, agent_type);

-- =============================================================================
-- CHANNELS  (matches `channels` table)
-- =============================================================================

CREATE TABLE channel (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    platform        channel_platform NOT NULL,
    -- api_config JSONB: provider-specific connection metadata (account_id, sender_id, etc).
    --   Secrets live in integration_credential, NOT here.
    api_config      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, name)
);

CREATE TRIGGER channel_set_updated_at BEFORE UPDATE ON channel
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_channel_tenant_platform ON channel(tenant_id, platform) WHERE is_active;

-- =============================================================================
-- CAMPAIGNS  (matches `campaigns` table)
-- =============================================================================

CREATE TABLE campaign (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    owner_id        UUID        REFERENCES app_user(id) ON DELETE SET NULL,
    name            VARCHAR(200) NOT NULL,
    status          campaign_status NOT NULL DEFAULT 'drafted',
    campaign_type   campaign_type NOT NULL,
    objective       TEXT        NOT NULL,
    -- budget total for the whole campaign; per-channel allocation in campaign_channel_budget
    budget_total    NUMERIC(14,2) NOT NULL DEFAULT 0,
    currency        CHAR(3)     NOT NULL DEFAULT 'USD',
    start_date      DATE        NOT NULL,
    end_date        DATE        NOT NULL,
    brief           TEXT,
    -- kpi_targets JSONB: { primary: "lead_count", target: 500, secondary: [{ metric, target }] }
    kpi_targets     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    launched_at     TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_date >= start_date),
    CHECK (budget_total >= 0)
);

CREATE TRIGGER campaign_set_updated_at BEFORE UPDATE ON campaign
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_campaign_tenant_status ON campaign(tenant_id, status);
CREATE INDEX idx_campaign_owner ON campaign(owner_id) WHERE owner_id IS NOT NULL;
CREATE INDEX idx_campaign_dates ON campaign(tenant_id, start_date, end_date);

-- Per-channel budget split (one campaign : many channels)
CREATE TABLE campaign_channel_budget (
    campaign_id     UUID        NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    channel_id      UUID        NOT NULL REFERENCES channel(id) ON DELETE RESTRICT,
    allocated       NUMERIC(14,2) NOT NULL CHECK (allocated >= 0),
    spent           NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (spent >= 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (campaign_id, channel_id)
);

CREATE TRIGGER campaign_channel_budget_set_updated_at BEFORE UPDATE ON campaign_channel_budget
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- AUDIENCES  (matches `audiences` table)
-- =============================================================================

CREATE TABLE audience (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    campaign_id     UUID        NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    -- segment_criteria JSONB: { include: [{ field, op, value }], exclude: [...], source: "crm|csv|enrichment" }
    segment_criteria JSONB      NOT NULL,
    estimated_size  INTEGER     CHECK (estimated_size IS NULL OR estimated_size >= 0),
    actual_size     INTEGER     CHECK (actual_size IS NULL OR actual_size >= 0),
    refreshed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER audience_set_updated_at BEFORE UPDATE ON audience
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_audience_campaign ON audience(campaign_id);

-- Materialised audience members (snapshot at refresh time)
CREATE TABLE audience_member (
    audience_id     UUID        NOT NULL REFERENCES audience(id) ON DELETE CASCADE,
    external_id     VARCHAR(200) NOT NULL,  -- CRM contact id, hashed email, etc
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (audience_id, external_id)
);

-- =============================================================================
-- TASKS  (matches `tasks` table)
-- =============================================================================

CREATE TABLE task (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    campaign_id     UUID        REFERENCES campaign(id) ON DELETE CASCADE,
    agent_id        UUID        NOT NULL REFERENCES agent(id) ON DELETE RESTRICT,
    parent_task_id  UUID        REFERENCES task(id) ON DELETE SET NULL,
    skill_name      VARCHAR(100) NOT NULL,  -- e.g. "seo_analysis", "copywriting", "segmentation"
    status          task_status NOT NULL DEFAULT 'queued',
    priority        SMALLINT    NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    attempt         SMALLINT    NOT NULL DEFAULT 0,
    max_attempts    SMALLINT    NOT NULL DEFAULT 3,
    -- input_data / output_data JSONB: agent-specific payload schemas, validated in app code
    input_data      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    output_data     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error_message   TEXT,
    scheduled_for   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER task_set_updated_at BEFORE UPDATE ON task
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_task_queue ON task(status, scheduled_for) WHERE status IN ('queued', 'awaiting_retry');
CREATE INDEX idx_task_campaign ON task(campaign_id) WHERE campaign_id IS NOT NULL;
CREATE INDEX idx_task_agent ON task(agent_id);

-- =============================================================================
-- CONTENT ASSETS  (matches `content_assets` table)
-- =============================================================================

CREATE TABLE content_asset (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    campaign_id     UUID        NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    channel_id      UUID        REFERENCES channel(id) ON DELETE SET NULL,
    asset_type      asset_type  NOT NULL,
    status          asset_status NOT NULL DEFAULT 'requested',
    title           VARCHAR(300),
    content         TEXT,        -- rendered final copy; rich payloads live in object storage
    -- metadata JSONB: { storage_uri, seo: { keywords[], score }, brand_check: { pass, notes } }
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    is_required     BOOLEAN     NOT NULL DEFAULT TRUE,
    scheduled_at    TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER content_asset_set_updated_at BEFORE UPDATE ON content_asset
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_content_asset_campaign_status ON content_asset(campaign_id, status);
CREATE INDEX idx_content_asset_channel ON content_asset(channel_id) WHERE channel_id IS NOT NULL;
CREATE INDEX idx_content_asset_search ON content_asset USING gin (title gin_trgm_ops);

-- Approval decisions (one asset : many decisions over its lifetime)
CREATE TABLE approval_decision_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    content_asset_id UUID       NOT NULL REFERENCES content_asset(id) ON DELETE CASCADE,
    reviewer_id     UUID        NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
    decision        approval_decision NOT NULL,
    reason          TEXT,
    edits           JSONB,       -- diff or revised copy if approved_with_edits
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_approval_decision_asset ON approval_decision_log(content_asset_id, decided_at DESC);

-- =============================================================================
-- A/B TESTS  (matches `ab_tests` table)
-- =============================================================================

CREATE TABLE ab_test (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    campaign_id     UUID        NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    hypothesis      TEXT,
    primary_metric  VARCHAR(50) NOT NULL,   -- e.g. "click_through_rate", "conversion_rate"
    status          ab_test_status NOT NULL DEFAULT 'designing',
    variant_a_id    UUID        REFERENCES content_asset(id) ON DELETE RESTRICT,
    variant_b_id    UUID        REFERENCES content_asset(id) ON DELETE RESTRICT,
    winner_id       UUID        REFERENCES content_asset(id) ON DELETE RESTRICT,
    confidence      NUMERIC(5,4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    started_at      TIMESTAMPTZ,
    stopped_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (variant_a_id IS NULL OR variant_b_id IS NULL OR variant_a_id <> variant_b_id),
    CHECK (winner_id IS NULL OR winner_id IN (variant_a_id, variant_b_id))
);

CREATE TRIGGER ab_test_set_updated_at BEFORE UPDATE ON ab_test
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_ab_test_campaign ON ab_test(campaign_id);

-- =============================================================================
-- ANALYTIC EVENTS  (matches `analytic_events` table)
-- =============================================================================

CREATE TABLE analytic_event (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    campaign_id     UUID        NOT NULL REFERENCES campaign(id) ON DELETE RESTRICT,
    channel_id      UUID        REFERENCES channel(id) ON DELETE SET NULL,
    content_asset_id UUID       REFERENCES content_asset(id) ON DELETE SET NULL,
    ab_test_id      UUID        REFERENCES ab_test(id) ON DELETE SET NULL,
    variant_id      UUID        REFERENCES content_asset(id) ON DELETE SET NULL,
    event_type      event_kind  NOT NULL,
    metric_value    NUMERIC(18,4),
    -- payload: provider-specific raw fields (utm_*, device, country, etc)
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    event_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hot read paths
CREATE INDEX idx_analytic_event_campaign_type_time
    ON analytic_event(campaign_id, event_type, event_at DESC);
CREATE INDEX idx_analytic_event_channel_time
    ON analytic_event(channel_id, event_at DESC) WHERE channel_id IS NOT NULL;
CREATE INDEX idx_analytic_event_variant
    ON analytic_event(variant_id, event_type) WHERE variant_id IS NOT NULL;

-- =============================================================================
-- AGENT LOGS  (matches `agent_logs` table) — APPEND ONLY
-- =============================================================================

CREATE TABLE agent_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    agent_id        UUID        NOT NULL REFERENCES agent(id) ON DELETE RESTRICT,
    task_id         UUID        REFERENCES task(id) ON DELETE RESTRICT,
    action          VARCHAR(100) NOT NULL,
    -- log_data JSONB: { model, input_tokens, output_tokens, latency_ms, tool_calls[], error }
    log_data        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    severity        VARCHAR(20) NOT NULL DEFAULT 'info',
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_log_agent_time ON agent_log(agent_id, logged_at DESC);
CREATE INDEX idx_agent_log_task ON agent_log(task_id) WHERE task_id IS NOT NULL;

-- =============================================================================
-- CROSS-CUTTING: AUDIT, INTEGRATIONS, COMPLIANCE
-- =============================================================================

-- Append-only system-wide audit log (every domain state change)
CREATE TABLE audit_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    actor_kind      VARCHAR(20) NOT NULL CHECK (actor_kind IN ('user', 'agent', 'system')),
    actor_id        UUID,
    entity_kind     VARCHAR(50) NOT NULL,  -- campaign, content_asset, audience, ab_test, ...
    entity_id       UUID        NOT NULL,
    action          VARCHAR(100) NOT NULL,  -- created, status_changed, approved, rejected, ...
    -- before / after capture the pre/post state diff
    before_state    JSONB,
    after_state     JSONB,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_log_entity ON audit_log(entity_kind, entity_id, logged_at DESC);
CREATE INDEX idx_audit_log_tenant_time ON audit_log(tenant_id, logged_at DESC);

-- External integration credentials (encrypted at app layer; this table holds opaque ciphertext)
CREATE TABLE integration_credential (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    channel_id      UUID        REFERENCES channel(id) ON DELETE CASCADE,
    provider        channel_platform NOT NULL,
    label           VARCHAR(200) NOT NULL,
    encrypted_payload BYTEA     NOT NULL,
    key_version     SMALLINT    NOT NULL DEFAULT 1,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel_id, label)
);

CREATE TRIGGER integration_credential_set_updated_at BEFORE UPDATE ON integration_credential
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Suppression list (CAN-SPAM / GDPR unsubscribe + bounce + complaint)
CREATE TABLE suppression_entry (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    channel_platform channel_platform NOT NULL,
    identifier      CITEXT      NOT NULL,  -- email, phone, social handle
    reason          VARCHAR(50) NOT NULL,   -- unsubscribe, bounce, complaint, manual
    source_event_id UUID        REFERENCES analytic_event(id) ON DELETE SET NULL,
    suppressed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel_platform, identifier)
);

CREATE INDEX idx_suppression_lookup ON suppression_entry(tenant_id, channel_platform, identifier);

-- =============================================================================
-- PHASE 2 TABLES (optimisation experiments, personalisation rules)
-- =============================================================================

CREATE TABLE optimisation_recommendation (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    campaign_id     UUID        NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    kind            VARCHAR(50) NOT NULL,  -- budget_shift, creative_swap, schedule_change
    -- proposal: { from: {...}, to: {...}, predicted_uplift, confidence }
    proposal        JSONB       NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'applied', 'rejected', 'expired')),
    applied_at      TIMESTAMPTZ,
    applied_by      UUID        REFERENCES app_user(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_optimisation_campaign ON optimisation_recommendation(campaign_id, status);

CREATE TABLE personalisation_rule (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    campaign_id     UUID        NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    -- match: { segment_filter: {...} }, render: { variables[], template_ref }
    match           JSONB       NOT NULL,
    render          JSONB       NOT NULL,
    priority        SMALLINT    NOT NULL DEFAULT 5,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER personalisation_rule_set_updated_at BEFORE UPDATE ON personalisation_rule
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- DEPLOY NOTES
-- =============================================================================
-- 1. After running this file, run the following as a superuser to enforce append-only:
--      REVOKE UPDATE, DELETE ON audit_log, agent_log, analytic_event FROM <app_role>;
-- 2. Row-level security (RLS) should be enabled per tenant on every domain table.
--    Policies are defined in a separate `rls.sql` (see E14-S04).
-- 3. Partition `analytic_event` by month once it exceeds ~50M rows.
-- =============================================================================
