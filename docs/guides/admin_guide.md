# MAS — Administration Guide

For admins installing, configuring, and operating the Marketing
Agentic System.

> If you're a marketer or manager *using* MAS, see the **User Guide**.

---

## Contents

1. [System overview + requirements](#1-system-overview--requirements)
2. [Installing — docker compose](#2-installing--docker-compose)
3. [Production deployment notes](#3-production-deployment-notes)
4. [Environment variables](#4-environment-variables)
5. [Database migrations](#5-database-migrations)
6. [OIDC / authentication](#6-oidc--authentication)
7. [Tenants + users](#7-tenants--users)
8. [Integrations](#8-integrations)
9. [Brand voice](#9-brand-voice)
10. [Compliance rules](#10-compliance-rules)
11. [Approval thresholds](#11-approval-thresholds)
12. [Frequency caps + rate limits + suppression](#12-frequency-caps--rate-limits--suppression)
13. [CAN-SPAM, unsubscribe, postal address](#13-can-spam-unsubscribe-postal-address)
14. [Observability + tracing](#14-observability--tracing)
15. [Backup + disaster recovery](#15-backup--disaster-recovery)
16. [Security model — RLS, credentials, secrets](#16-security-model--rls-credentials-secrets)
17. [Troubleshooting](#17-troubleshooting)
18. [Upgrade procedure](#18-upgrade-procedure)
19. [Useful CLI commands](#19-useful-cli-commands)

---

## 1. System overview + requirements

### Stack
- **Application:** Python 3.12, FastAPI, served by uvicorn
- **Database:** PostgreSQL 15+ (Row-Level Security required)
- **Worker:** in-process durable task queue (no Celery / Redis cluster)
- **Auth:** OIDC (any compliant IdP — Google Workspace, Okta, Azure AD, Auth0)
- **Optional:** OpenTelemetry collector for tracing; Mailpit for local SMTP

### Minimum requirements
| Resource    | Single-tenant prod  | Multi-tenant SaaS (10 tenants)        |
|-------------|---------------------|----------------------------------------|
| App pods    | 2 (HA)              | 4+                                     |
| Worker pods | 1                   | 2–3                                    |
| Postgres    | 4 vCPU / 16 GB RAM  | 8 vCPU / 32 GB / read replica          |
| Storage     | 100 GB SSD          | 500 GB SSD + nightly snapshots         |

### External dependencies (optional, per-tenant)
- **Anthropic** API key — required for the Strategist + Content Creator (LLM-backed tools won't register without it)
- **SendGrid** — email dispatch
- **HubSpot** — CRM contact ingest
- **Plausible** — web analytics
- **LinkedIn / X / Meta** — social publishing
- **Google Ads / Meta Ads** — (scaffolded; full ingest is a polish unit)

---

## 2. Installing — docker compose

The fastest way to get MAS running locally.

```bash
git clone <repo> mas
cd mas
cp .env.example .env.local          # fill in real values for prod
docker compose up -d                # postgres, oidc-mock, mailpit, otel-collector
uv run alembic upgrade head         # apply 22 migrations
uv run uvicorn app.api.app:app --host 0.0.0.0 --port 8001
```

Open `http://localhost:8001/api/auth/login` to sign in through
oidc-mock.

### Services compose brings up
| Service        | Port    | Purpose                                    |
|----------------|---------|--------------------------------------------|
| postgres       | 5434    | The MAS database                           |
| oidc-mock      | 9000    | Local IdP for development                  |
| mailpit        | 8025    | Local SMTP viewer (browse sent emails)     |
| otel-collector | 4317/8  | OTLP receiver — forward to your tracing UI |

### Stopping
```bash
docker compose down               # keeps volumes (your dev data)
docker compose down -v            # wipes volumes (fresh start)
```

---

## 3. Production deployment notes

Single-tenant deployment shape (the recommended starting point):

```
       ┌──────────────────────────────────────────────┐
       │  Load balancer (TLS termination)             │
       └─────────────────┬────────────────────────────┘
                         │
        ┌────────────────▼──────────────────┐
        │  MAS app pods (N)  + worker pods  │   ← stateless; scale horizontally
        └─────────────┬─────────────────────┘
                      │
                      ▼
            ┌──────────────────────┐
            │  Postgres 15 (RLS)   │   ← managed (RDS / Cloud SQL) recommended
            └──────────────────────┘
```

### Container image
Build from the included `Dockerfile`:

```bash
docker build -t mas:$(git rev-parse --short HEAD) .
```

Push to your registry; run with `uvicorn app.api.app:app
--host 0.0.0.0 --port 8001 --workers 4`.

### Worker process
The durable task queue runs **inside the same Python image**. In prod
either:

- **Same container** — let the FastAPI app process workers in a
  background thread. Simpler; fine up to medium load.
- **Separate worker pods** — run `uv run python -m app.orchestrator.worker_loop`
  as its own deployment. Recommended once dispatch volume crosses
  a few thousand sends/day.

### TLS, sessions, and reverse proxy
- Terminate TLS at the load balancer or ingress; MAS speaks plain HTTP
  internally.
- Set `SESSION_SECRET` to a 32-byte random value.
- If MAS is behind a proxy, configure the proxy to set
  `X-Forwarded-Proto: https` so session cookies get the `Secure` flag.

### Multi-tenant SaaS deployment
- One Postgres database; tenants isolated by RLS.
- Run Alembic once globally — never per tenant.
- Encrypted per-tenant credentials in `integration_credential`.
- Per-tenant rate limits in `provider_rate_limit`.

---

## 4. Environment variables

Configured via `.env.local` (loaded by pydantic-settings) or actual env
vars. **Real environment variables override .env.local.**

### Core (always required)
| Var               | Description                                                       |
|-------------------|-------------------------------------------------------------------|
| `DATABASE_URL`    | Postgres connection URI, e.g. `postgresql+asyncpg://...`          |
| `SESSION_SECRET`  | Cookie signing key (32+ random bytes)                             |
| `CREDENTIALS_SECRET` | Fernet key for per-tenant credential encryption. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

### OIDC
| Var | Notes |
|-----|-------|
| `OIDC_ISSUER`         | Your IdP's issuer URL                       |
| `OIDC_CLIENT_ID`      | Client ID registered with the IdP            |
| `OIDC_CLIENT_SECRET`  | Client secret (store securely)               |
| `OIDC_REDIRECT_URI`   | `https://your.host/api/auth/callback`        |

### LLM
| Var | Notes |
|-----|-------|
| `ANTHROPIC_API_KEY`   | Required for Strategist + Content Creator. Without it, those tools don't register (degraded but safe). |
| `COPYWRITING_MODEL`   | Default `claude-sonnet-4-6`. Override for higher quality (`claude-opus-4-7`) or cheaper drafts. |
| `STRATEGIST_MODEL`    | Same; default sonnet 4-6.                    |

### Optional integrations (set per tenant via UI usually; env vars only for shared client IDs)
| Var                       | When                                       |
|---------------------------|---------------------------------------------|
| `HUBSPOT_CLIENT_ID/SECRET` | OAuth app credentials for HubSpot connector |
| `LINKEDIN_CLIENT_ID/SECRET` | OAuth app credentials for LinkedIn         |
| `X_CLIENT_ID/SECRET`        | OAuth app credentials for X (Twitter)      |
| `META_CLIENT_ID/SECRET`     | OAuth app credentials for Meta (Facebook)  |
| `PLAUSIBLE_API_KEY/SITE_ID` | Web analytics ingest                       |

### Operational
| Var | Notes |
|-----|-------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP gRPC endpoint (default `http://localhost:4317`) |
| `OTEL_SERVICE_NAME`           | Service label in traces (default `mas-api`) |
| `AUDIENCE_MEMBER_FRESHNESS_TTL_DAYS` | Default 30. Refresh prompt cadence. |
| `DEV_IMPERSONATION_ENABLED`   | **Set to `false` in prod.** Default false. Enables `/api/auth/dev-impersonate` for browser walkthroughs. |

### Never commit
- `.env.local` (in `.gitignore`)
- Real `ANTHROPIC_API_KEY`, OIDC secrets, provider credentials
- The `CREDENTIALS_SECRET` Fernet key

---

## 5. Database migrations

Alembic-managed schema. **22 migrations** through W41.

```bash
uv run alembic current          # show current revision
uv run alembic history          # all revisions
uv run alembic upgrade head     # apply pending
uv run alembic downgrade -1     # roll back one (use sparingly)
```

### Migration safety rules
- Migrations are **forward-only in prod**. Test downgrade paths in
  staging only.
- The `mas_app` role used by the application has no DDL privileges.
  Migrations run as the database owner (`mas`).
- `audit_log` and `agent_log` have `UPDATE` / `DELETE` revoked — they
  are append-only by design. Never write a migration that mutates
  historical rows.

### Adding a migration (engineering ops)
```bash
uv run alembic revision -m "your description"
# Edit the generated file under app/db/migrations/versions/
uv run alembic upgrade head
```

---

## 6. OIDC / authentication

MAS uses **OIDC authorization-code flow**. Any compliant IdP works.

### Google Workspace
1. Create an OAuth 2.0 Client ID in Google Cloud Console.
2. Authorized redirect URI: `https://your.host/api/auth/callback`.
3. Set scopes: `openid email profile`.
4. Note the **Hosted Domain** (`hd`) claim — MAS uses this to map
   incoming logins to the correct tenant (see `tenant.oidc_hosted_domain`).
5. Set env vars (`OIDC_*`).

### Okta / Auth0 / Azure AD
Same shape. The only quirk: ensure the IdP issues an `hd`-equivalent
custom claim. If not, set `tenant.oidc_hosted_domain` to a value the IdP
includes (or pin the user to a tenant another way — see
[Tenants + users](#7-tenants--users)).

### Local dev — oidc-mock
The `docker-compose.yml` ships a mock OIDC provider that issues
hardcoded claims. **Never use in prod.**

### Dev impersonation
For browser walkthroughs without going through OIDC at all:

```bash
# .env.local
DEV_IMPERSONATION_ENABLED=true
```

Then visit `/api/auth/dev-impersonate?email=alex@acme.test`. Returns
404 unless the flag is on (so it can't leak into prod even if the
endpoint is hit).

---

## 7. Tenants + users

### Creating a tenant
There's no public "self-serve signup" today. Tenants are created via
admin Python script or seed:

```python
# scripts/create_tenant.py (write your own; pattern from scripts/seed_live_demo.py)
async with SessionLocal() as session, session.begin():
    tenant = Tenant(
        name="Acme Corp",
        oidc_hosted_domain="acme.com",
    )
    session.add(tenant)
    await session.flush()
    session.add(
        AppUser(
            tenant_id=tenant.id,
            email="admin@acme.com",
            role=UserRole.admin,
            is_active=True,
        )
    )
```

### Auto-provisioning
The OIDC callback auto-creates an `app_user` row with role `viewer`
the first time someone from a known tenant signs in. Upgrade their role
manually:

```sql
UPDATE app_user SET role = 'admin' WHERE email = 'alex@acme.com';
```

### Roles
- `viewer` — read everything
- `marketer` — create + edit campaigns, accept recommendations
- `manager` — approve content, stop A/B tests, dispute reconciliation
- `admin` — everything: integrations, OIDC config, anomaly dismissal,
  reconciliation, key rotation

Higher roles satisfy lower-role requirements (W3).

### Deactivating a user
```sql
UPDATE app_user SET is_active = false WHERE email = 'former@acme.com';
```

The session middleware refuses inactive users on the next request.

---

## 8. Integrations

All per-tenant credentials encrypt at rest with Fernet. Stored in
`integration_credential.encrypted_payload`.

### SendGrid (email)
1. In SendGrid, create an API key with **Mail Send** permission.
2. Add Verified Senders for every `from_email` you'll use.
3. From MAS admin UI (or the `/api/integrations/email/sendgrid`
   endpoint), submit:
   - `api_key`
   - `default_from_email`
   - `verified_senders` (list)
   - `webhook_secret` (random; same value will be required in the
     SendGrid webhook URL header)
4. Configure SendGrid event webhook pointing at
   `https://your.host/webhooks/sendgrid/{tenant_id}` with the
   `X-MAS-Webhook-Secret` header set to `webhook_secret`.

### HubSpot
1. Create a HubSpot OAuth app. Scopes: `crm.objects.contacts.read`,
   `crm.lists.read`.
2. Set `HUBSPOT_CLIENT_ID` / `HUBSPOT_CLIENT_SECRET` env vars.
3. Tenants connect via `/api/integrations/hubspot/connect` — OAuth
   round-trip, tokens stored encrypted.

### LinkedIn (W30 — full impl)
1. Create a LinkedIn Marketing Developer app with `r_organization_social` +
   `w_organization_social` + `rw_organization_admin` scopes.
2. Set `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET`.
3. Tenants connect via `/api/integrations/social/linkedin/connect`.

### X (W40) + Meta (W40)
Same shape as LinkedIn. OAuth client credentials in env; per-tenant
tokens stored encrypted.

### Plausible (web analytics)
Set `PLAUSIBLE_API_KEY` + `PLAUSIBLE_SITE_ID` per tenant (no OAuth —
API key based). Configure UTM tagging on outbound URLs so MAS can
attribute clicks to campaigns.

### Google Ads / Meta Ads (W40 — scaffolded)
OAuth + account listing work. Campaign upsert + spend ingest call
`NotImplementedError` until the follow-up unit lands. **Don't depend
on automated spend reporting from these providers yet.**

### Webhook receiver
Single endpoint: `POST /webhooks/{provider}/{tenant_id}`. Every webhook
lands in `raw_webhook` regardless of signature validity. Verify webhook
configuration via `GET /api/webhooks/unmapped` — admin-only surface
that lists webhooks that didn't map to an `analytic_event`.

### Per-tenant rate limits
`provider_rate_limit` table (W31). Set per (tenant, provider) so a
runaway campaign can't blow your provider quota:

```sql
INSERT INTO provider_rate_limit
  (tenant_id, provider, requests_per_minute, enabled)
VALUES
  ('<tenant-id>', 'sendgrid', 60, true);
```

Or via the admin endpoint: `PUT /api/provider-rate-limits/{provider}`.

---

## 9. Brand voice

`brand_voice` table — one row per tenant.

Fields:
- `tone` — short text (e.g. "Direct, technical, confident — no
  marketing fluff")
- `audience_persona` — sentence-level description of the target reader
- `forbidden_phrases` — array of phrases never to use (e.g.
  ["solutions provider", "leverage synergies"])
- `signature` — closing block appended to email assets

Configured via the admin UI (Settings → Brand Voice) or directly.
The Content Creator reads it on every generation.

**Tip:** spend real time on this when you onboard a tenant. The voice
config drives copy quality more than any other input.

---

## 10. Compliance rules

`compliance_rule` table (W23). Per-tenant. Each rule is:

| Field            | Notes                                                      |
|------------------|------------------------------------------------------------|
| `pattern_kind`   | `literal` / `regex` / `fuzzy`                              |
| `keyword`        | The match string or pattern                                 |
| `severity`       | `warning` / `blocker`                                       |
| `description`    | Human-readable reason for the marketer review              |

**Severity behaviour:**
- `warning` — asset still goes through approval; reviewer sees the flag
- `blocker` — asset moves to `failed` before reaching approval. Manager
  or admin can override and re-run.

**Common rules to load on tenant setup:**

| Keyword              | Pattern  | Severity | Rationale                              |
|----------------------|----------|----------|-----------------------------------------|
| `guaranteed return`  | literal  | blocker  | FTC + financial regs                    |
| `cure`               | literal  | blocker  | FDA / healthcare regs                   |
| `free!`              | literal  | warning  | Spam-filter trigger                     |
| `act now`            | fuzzy    | warning  | High-pressure language                  |
| `click here`         | literal  | warning  | Phishing-adjacent + bad UX              |

CRUD via the admin UI or `/api/compliance/rules`.

---

## 11. Approval thresholds

`tenant_approval_settings` table (W26). One row per tenant.

| Field                            | Effect                                                    |
|----------------------------------|------------------------------------------------------------|
| `auto_approve_below_score`       | Compliance score threshold. Drafts below this auto-pass; at or above land in the approval queue. |
| `required_approver_role`         | `manager` or `admin`                                       |
| `batch_approval_enabled`         | True → approve multiple assets in one action               |

Default behaviour: every draft requires a `manager` approval. Tighten
or loosen per tenant.

Set via `PUT /api/approval-settings` (admin only).

---

## 12. Frequency caps + rate limits + suppression

### Frequency caps (`frequency_cap_setting`)
Per-tenant, per-channel maximum sends to the same recipient within a
window.

```sql
INSERT INTO frequency_cap_setting
  (tenant_id, channel_platform, max_sends, window_hours, enabled)
VALUES
  ('<tenant-id>', 'email', 3, 168, true);  -- 3 emails / week max
```

Dispatch refuses sends that would exceed the cap; the dispatch_attempt
lands as `skipped` with `last_error=frequency_cap`.

### Provider rate limits (`provider_rate_limit`)
Throttle outbound API calls to providers. Set per (tenant, provider):

```sql
INSERT INTO provider_rate_limit (tenant_id, provider, requests_per_minute, enabled)
VALUES ('<tenant-id>', 'sendgrid', 60, true);
```

### Suppression list (`suppression_entry`)
Append-only blocklist. Auto-populated by:
- Bounces (hard bounces from SendGrid)
- Spam complaints
- Unsubscribe webhook fires

Manual entries supported via `/api/suppression`.

Dispatch skips recipients on the suppression list before frequency
caps or rate limits kick in.

---

## 13. CAN-SPAM, unsubscribe, postal address

`tenant_compliance_settings` table (W29). Per-tenant:

| Field                              | Notes                                              |
|------------------------------------|-----------------------------------------------------|
| `postal_address`                   | Physical address; appears in CAN-SPAM footer       |
| `unsubscribe_secret`               | Signs the unsubscribe URL. Rotate to invalidate    |
| `auto_pause_on_critical_anomaly`   | Opt-in: pause campaign after 2 consecutive crit anomalies (W37) |

**MAS auto-injects** the postal address + signed unsubscribe URL on
every email send (W29, E16-S04). Missing postal_address → footer
falls back to a placeholder with a warning in the dispatch metadata
(you'll see it in audit_log; fix it before going live).

**Unsubscribe URL pattern**: `https://your.host/unsubscribe?token=<signed>`. Public — no
session required.

---

## 14. Observability + tracing

OpenTelemetry built-in (W9).

### Auto-instrumented
- FastAPI (request spans)
- SQLAlchemy (query spans)
- Anthropic client (LLM call spans)

### Configuration
| Var                          | Default                          |
|------------------------------|----------------------------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317`          |
| `OTEL_SERVICE_NAME`           | `mas-api`                        |

Plug any OTLP-compatible backend: Honeycomb, Datadog, Jaeger,
Grafana Tempo, Sentry, etc.

### Useful spans + attributes
- `actor.id`, `actor.kind` — set on every request span (middleware)
- `campaign.id`, `tenant.id` — set when handlers know them
- Tool names — Anthropic spans tagged with `tool.name=copywriting.generate` etc.

### Audit log
Beyond OTEL traces, every privileged action writes to `audit_log`:
- Approvals + rejections
- State transitions
- Promotion of A/B winners
- Anomaly dismissals
- Recommendation accept/reject
- Spend reconciliation actions

Query for forensics:
```sql
SELECT * FROM audit_log
 WHERE tenant_id = '...'
   AND created_at > now() - interval '7 days'
 ORDER BY created_at DESC;
```

---

## 15. Backup + disaster recovery

### What to back up
- **Postgres** — full snapshot daily, WAL archive for PITR
- **Secrets** — `CREDENTIALS_SECRET` (key for decrypting integration
  credentials) is the only thing you absolutely cannot lose. Without
  it, every per-tenant API key in the DB is unrecoverable.

### What doesn't need backup
- App container image (rebuildable)
- `.env.local` (rebuildable; secrets are the only sensitive bit)
- OIDC config (declarative)

### Restore drill
1. Provision fresh Postgres at the snapshot point
2. Restore `CREDENTIALS_SECRET` from your secrets manager
3. Deploy the app pointing at the new DB
4. Verify `/api/me` round-trip
5. Verify one webhook receiver works (no signature drift)

### Tenant-level export
Currently no built-in "export tenant" API. For GDPR-style requests:

```sql
-- Export everything for a tenant
\copy (SELECT * FROM campaign WHERE tenant_id='...') TO 'campaign.csv' CSV HEADER;
-- Repeat for other domain tables.
```

A proper data-portability surface is on the roadmap.

---

## 16. Security model — RLS, credentials, secrets

### Multi-tenant isolation
Every domain table has a Postgres row-level security policy keyed on
`current_setting('app.tenant_id')`. The application sets it inside
each transaction:

```python
async def set_tenant_context(session, tenant_id):
    await session.execute(text("SET LOCAL ROLE mas_app"))
    await session.execute(text(
        "SELECT set_config('app.tenant_id', :tid, true)"
    ), {"tid": str(tenant_id)})
```

The `mas_app` role is **not** the migration owner. Even raw SQL injected
through application code can't bypass the policy.

### Credential encryption
Per-tenant API keys (SendGrid, HubSpot, LinkedIn, etc.) are encrypted
with Fernet keyed by `CREDENTIALS_SECRET`. Stored in
`integration_credential.encrypted_payload`. Cleartext never appears in
`audit_log` (the audit listener strips encrypted blobs).

### Key rotation
1. Generate a new Fernet key.
2. Run a one-shot script that decrypts every `integration_credential`
   row with the old key, re-encrypts with the new key, and bumps
   `key_version`.
3. Roll `CREDENTIALS_SECRET` env var.

No downtime if `key_version` is checked at decrypt time (it is —
`integration_credential.key_version` column).

### Append-only audit
`audit_log` and `agent_log` have `UPDATE` + `DELETE` revoked from the
`mas_app` role. Tampering with history requires DB-owner credentials.

### Webhook security
- Every webhook lands in `raw_webhook` regardless of signature validity
  (W33, E12-S06).
- Failed signatures write `audit_log` action `webhook_signature_failed`
  with provider + reason in metadata.
- Sensitive headers (`x-mas-webhook-secret`, `authorization`, `cookie`)
  are redacted in the persisted snapshot.

### LLM safety
- Only the **Strategist** + **Content Creator** call Anthropic.
- Every output goes through `compliance_rule` keyword scanning + the
  approval gate before publishing.
- LLM costs bounded by `max_tokens` per call (1024 for copy, 2048 for
  strategy).
- Without `ANTHROPIC_API_KEY`, those tools simply don't register —
  the app boots in degraded-but-safe mode (no auto-content; everything
  else works).

---

## 17. Troubleshooting

### "OIDC callback returns 500"
The IdP didn't include the expected claims. Common causes:
- Missing `email` or `hd` claim — check your IdP's claim mapping
- Mismatched `OIDC_REDIRECT_URI` between IdP and `.env.local`
- Clock skew → `jose.errors.ExpiredTokenError`

Quick diagnostic:
```bash
curl -s -X POST $OIDC_ISSUER/token \
     -d "grant_type=client_credentials&client_id=$CID&client_secret=$CS&scope=openid+email+profile" \
  | python3 -m json.tool
```
The id_token's payload should have `sub`, `email`, and `hd`.

### "All dispatches fail with `no_credential`"
Tenant has no `integration_credential` row for the channel's
provider. Configure SendGrid (email) / LinkedIn (social) / etc.

### "Anomaly detector returns nothing"
Needs ≥ 14 distinct days of `analytic_event` data inside the rolling
window. New campaigns won't fire anomalies until they accumulate
baseline data.

### "A/B test stuck in `running` past max_runtime"
The evaluator is throttled to once per 15 minutes. If you need an
immediate evaluation, call `evaluate_test()` from a Python REPL or
wait for the next cycle.

### "Recommendation engine returns nothing"
- Campaign age < 5 days
- Channels too close (CPO ratio < 1.2×)
- One channel has zero conversions AND zero clicks
- An identical pending recommendation already exists (dedupe)

### "Approval queue is empty but assets are pending"
The auto-approve threshold may be too permissive — assets are passing
through without human review. Check `tenant_approval_settings.auto_approve_below_score`.

### "Webhooks not mapping to events"
- Check `GET /api/webhooks/unmapped` — that's the surface for
  diagnosing provider integrations
- Verify the webhook `provider` query path matches the registered
  provider name (`sendgrid`, `linkedin`)
- Verify the X-MAS-Webhook-Secret matches the credential's
  `webhook_secret`

### "Worker is restarting / stuck"
Check `task` table for rows with `status='running'` and stale
`leased_until`. The lease is reclaimable after expiry; restart the
worker to pick them up.

### "I rotated `SESSION_SECRET` and everyone got logged out"
Expected. Session cookies are signed with the secret. Communicate the
rotation in advance.

### "I rotated `CREDENTIALS_SECRET` and integrations broke"
Did you run the re-encryption script? See [Key rotation](#key-rotation).

---

## 18. Upgrade procedure

1. Read `CHANGELOG.md` for the target version
2. In staging:
   - `git pull && uv sync`
   - `uv run alembic upgrade head`
   - `uv run pytest --no-header -q` (746 tests; full suite ~50s)
   - Smoke-test critical paths
3. In prod (rolling, zero-downtime):
   - `alembic upgrade head` against prod DB (forward-compatible)
   - Roll app pods one at a time
   - Restart worker
4. Watch error rate + audit_log for ~10 minutes

If a migration is **NOT** forward-compatible (rare; flagged in the
release notes), schedule downtime and run the migration during the
maintenance window.

---

## 19. Useful CLI commands

| Command                                                            | Purpose                                          |
|--------------------------------------------------------------------|--------------------------------------------------|
| `uv run alembic current`                                           | Show current migration revision                  |
| `uv run alembic upgrade head`                                      | Apply pending migrations                         |
| `uv run pytest --no-header -q`                                     | Full test suite (746 tests, ~50s)               |
| `uv run python -m scripts.seed_live_demo`                          | Seed a demo campaign into the dev DB             |
| `uv run python -m scripts.full_demo`                               | End-to-end pipeline against a fresh testcontainer |
| `uv run uvicorn app.api.app:app --host 0.0.0.0 --port 8001`        | Start the app                                    |
| `docker compose up -d`                                             | Start dev dependencies                           |
| `docker compose logs -f mas-postgres`                              | Tail Postgres logs                               |
| `psql $DATABASE_URL`                                               | Connect to the DB directly                       |

### Useful SQL queries

```sql
-- Active campaigns per tenant
SELECT t.name, c.name, c.status, c.start_date, c.end_date
  FROM campaign c JOIN tenant t ON t.id = c.tenant_id
 WHERE c.status IN ('live','optimising','paused')
 ORDER BY c.start_date DESC;

-- Stuck dispatches (in 'sent' for > 1 hour with no provider_message_id)
SELECT id, content_asset_id, recipient_identifier, sent_at, last_error
  FROM dispatch_attempt
 WHERE status = 'failed' AND sent_at > now() - interval '24 hours'
 ORDER BY sent_at DESC LIMIT 50;

-- Anomaly volume by tenant + metric (last 7 days)
SELECT t.name, ma.metric, ma.severity, count(*)
  FROM metric_anomaly ma JOIN tenant t ON t.id = ma.tenant_id
 WHERE ma.created_at > now() - interval '7 days'
 GROUP BY t.name, ma.metric, ma.severity
 ORDER BY count DESC;

-- Pending approvals per tenant
SELECT t.name, count(*)
  FROM content_asset ca JOIN tenant t ON t.id = ca.tenant_id
 WHERE ca.status = 'pending_approval'
 GROUP BY t.name;

-- Unmapped webhooks last 24h (diagnostic for broken provider integrations)
SELECT provider, count(*), max(received_at)
  FROM raw_webhook
 WHERE mapped_event_id IS NULL
   AND received_at > now() - interval '24 hours'
 GROUP BY provider;
```

---

## Need more?

- **User Guide** — for marketers and managers using MAS
- **Technical Deep Dive** ([deck](../marketing/deck_technical.md)) — full system architecture
- **API docs** — `/docs` on a running instance (interactive OpenAPI)

For escalation: gather the offending campaign id, tenant id, and an
approximate timestamp before opening a ticket. `audit_log` makes the
forensic path short.
