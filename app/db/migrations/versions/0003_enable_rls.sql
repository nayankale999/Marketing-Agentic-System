-- 0003: Tenant isolation via Row-Level Security.
--
-- Pattern:
--   - Migrations + admin work run as the table owner (bypasses RLS naturally,
--     because we do NOT use FORCE ROW LEVEL SECURITY).
--   - The app, per request, executes `SET LOCAL ROLE mas_app` and stamps the
--     session var `app.tenant_id` via set_config('app.tenant_id', <uuid>, true).
--   - `mas_app` is NOLOGIN; it's reachable only via SET ROLE from the owner.
--   - Policies USING (tenant_id = current_setting('app.tenant_id', true)::uuid).
--     When the setting is unset, current_setting(..., true) returns NULL,
--     NULL::uuid yields NULL, NULL = X is NULL, the policy rejects -> 0 rows.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mas_app') THEN
        CREATE ROLE mas_app NOLOGIN NOBYPASSRLS;
    END IF;
END$$;

GRANT mas_app TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO mas_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mas_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mas_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mas_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO mas_app;

-- Enable RLS on every domain table.
ALTER TABLE tenant                      ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_user                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent                       ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_channel_budget     ENABLE ROW LEVEL SECURITY;
ALTER TABLE audience                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE audience_member             ENABLE ROW LEVEL SECURITY;
ALTER TABLE task                        ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_asset               ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_decision_log       ENABLE ROW LEVEL SECURITY;
ALTER TABLE ab_test                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytic_event              ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_log                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_credential      ENABLE ROW LEVEL SECURITY;
ALTER TABLE suppression_entry           ENABLE ROW LEVEL SECURITY;
ALTER TABLE optimisation_recommendation ENABLE ROW LEVEL SECURITY;
ALTER TABLE personalisation_rule        ENABLE ROW LEVEL SECURITY;

-- tenant: the row IS the tenant -> match by id.
CREATE POLICY tenant_isolation ON tenant
    USING (id = current_setting('app.tenant_id', true)::uuid);

-- Tables with a direct tenant_id column.
CREATE POLICY tenant_isolation ON app_user
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON agent
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON channel
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON campaign
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON audience
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON task
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON content_asset
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON ab_test
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON analytic_event
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON agent_log
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON audit_log
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON integration_credential
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON suppression_entry
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON optimisation_recommendation
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation ON personalisation_rule
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- Child tables: reach the parent via subquery, recursive RLS does the rest.
CREATE POLICY tenant_isolation ON audience_member
    USING (audience_id IN (SELECT id FROM audience));
CREATE POLICY tenant_isolation ON approval_decision_log
    USING (content_asset_id IN (SELECT id FROM content_asset));
CREATE POLICY tenant_isolation ON campaign_channel_budget
    USING (campaign_id IN (SELECT id FROM campaign));
