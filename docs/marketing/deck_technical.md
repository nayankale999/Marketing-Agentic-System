<!-- Technical deep-dive deck. Render with Marp / Pandoc-Beamer / paste into Slides.
     Slides separated by `---`. Audience is engineering/architecture leads,
     security reviewers, infra/platform owners. -->

# MAS — Technical Deep Dive

**Marketing Agentic System — architecture, security, infrastructure**

For: engineering, security, and infrastructure reviewers
Updated: 2026-05-24

> Notes: Open with the audience. This deck is meant for the team doing the security review and the architect who has to integrate MAS into your platform. Skip the value-prop language — it's covered in the executive deck.

---

# Agenda

1. Value framing (1 slide)
2. System context + agent map
3. Data model + multi-tenant isolation
4. Approval + compliance guardrails
5. Access control
6. Tool layer + LLM safety
7. Distribution + idempotency
8. Analytics + observability
9. Infrastructure + deployment
10. Test harness + CI
11. Roadmap + open questions

> Notes: 11 sections, ~22 slides total. Pace ~1.5 min/slide for a 30-min review meeting.

---

# Value framing (1 slide)

A marketing team running one campaign touches 6–10 tools and spends half the week stitching them together. MAS replaces the stitching with six specialist agents on one product.

**Not a copywriting tool.** A marketing operations product with copy + analytics + dispatch + compliance built in.

> Notes: Single slide, then move on. The buyer is here for the engineering details.

---

# System context

```
                       ┌─────────────────────────────────────┐
                       │   Marketing User (browser, OIDC)    │
                       └─────────────┬───────────────────────┘
                                     │ HTTPS
                       ┌─────────────▼───────────────────────┐
                       │   FastAPI API + HTMX/Jinja UI       │
                       └──────┬───────────────────┬──────────┘
                              │                   │
        ┌─────────────────────▼────┐   ┌──────────▼──────────────┐
        │  Orchestrator + 6 Agents │   │  Postgres 15 (RLS-on)   │
        │  + Tool Registry         │◄──┤  audit_log, agent_log,  │
        └────┬──────────┬──────────┘   │  campaign, audience,    │
             │          │              │  analytic_event, etc.   │
             ▼          ▼              └─────────────────────────┘
   ┌─────────────┐  ┌────────────┐
   │  Anthropic  │  │ Connectors │   ── SendGrid · LinkedIn · X · Meta
   │  (LLM)      │  │            │      Plausible · HubSpot · Google Ads*
   └─────────────┘  └────────────┘     (* scaffolded; full impl deferred)
```

> Notes: Walk top to bottom. The key takeaway is that **the LLM is one of two outbound dependencies** — it can be swapped, throttled, or disabled per tenant. Anthropic is not on the critical path for non-LLM operations.

---

# Agent map

| Agent                       | Slice  | Entry point                                                                | LLM-backed? |
|-----------------------------|--------|----------------------------------------------------------------------------|-------------|
| Audience Targeting          | 2 (W15)| `app.agents.audience_targeting.materialise`                                | No          |
| Campaign Strategist         | 3 (W20)| `app.agents.strategist.propose`                                            | Yes         |
| Content Creator             | 3 (W22)| `app.agents.content_creator.generate_asset`                                | Yes         |
| Approval Orchestrator       | 3 (W25)| `app.agents.approval.*` + state-machine transitions                         | No          |
| Channel Distribution        | 4 (W28)| `app.agents.distribution.dispatch_email_asset` / `dispatch_social_asset`   | No          |
| Analytics & Optimisation    | 5 (W37)| `app.agents.analytics.analyse_campaign`                                    | Mostly no   |

Each agent is a Python module with a documented surface. The orchestrator state machine wires them together via state transitions on `campaign.status`.

> Notes: Buyers worry about LLM hallucinations affecting their data. Highlight that only 2 of 6 agents call an LLM, and both go through a tool registry with respx-mockable contracts.

---

# State machine

```
drafted → audience_built → strategy_set → content_in_production →
approval_pending → ready_to_launch → live → optimising
                                     ↕ pause/resume
                                  paused
                                     ↓
                                completed
```

- All transitions are explicit; each has a `guard` (precondition) and an optional `on_enter` hook.
- The `pause` transition cancels queued tasks but lets running ones drain.
- `complete_campaign` auto-generates an end-of-campaign report.

**File:** [`app/orchestrator/state_machine.py`](../../app/orchestrator/state_machine.py)

> Notes: Show the diagram, then call out the pause/resume semantics — that's the AC most reviewers ask about.

---

# Data model — core

11 first-class tables. Every domain table carries `tenant_id`. Composite uniqueness keys prevent cross-tenant collisions:

```
tenant ─── app_user (UNIQUE tenant_id, email)
   │
   ├── channel (UNIQUE tenant_id, name)
   ├── campaign ─┬── audience ──── audience_member
   │             ├── strategy_proposal ─── strategy_touchpoint
   │             ├── content_asset ─── approval_decision_log
   │             ├── ab_test ──── ab_test_assignment
   │             ├── analytic_event
   │             └── campaign_channel_budget
   │
   ├── integration_credential (encrypted blob)
   ├── audit_log + agent_log (append-only)
   └── … (22 migrations total)
```

> Notes: Buyers ask "where does my data live?" — this slide is the answer. Reference the migration count (22) as a proxy for maturity. Most production systems are in this range after a year of work.

---

# Tenant isolation (RLS)

Every domain table has a Postgres row-level security policy:

```sql
ALTER TABLE campaign ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON campaign
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
```

The application sets the GUC inside each transaction:

```python
async def set_tenant_context(session, tenant_id):
    await session.execute(text("SET LOCAL ROLE mas_app"))
    await session.execute(text(
        "SELECT set_config('app.tenant_id', :tid, true)"
    ), {"tid": str(tenant_id)})
```

- `SET LOCAL` is transaction-scoped → no leak across tx boundaries
- `mas_app` role is **not** the migration owner; can't drop tables
- `audit_log` + `agent_log` are append-only — UPDATE/DELETE revoked

**File:** [`app/db/session.py`](../../app/db/session.py)

> Notes: This is the #1 question from any security reviewer. RLS at the database level means a bug in the application layer cannot leak data — even raw SQL would be filtered. Show the policy code; it's 5 lines.

---

# Credential encryption

Per-tenant API keys (SendGrid, HubSpot, LinkedIn, etc.) land in `integration_credential.encrypted_payload`:

```python
encrypted = get_encrypted_payload().encrypt(payload)
session.add(IntegrationCredential(
    tenant_id=tenant_id,
    provider="sendgrid",
    encrypted_payload=encrypted,
    key_version=1,
))
```

- Fernet (cryptography library), key rotated via `key_version`.
- Master key from env: `CREDENTIALS_SECRET` — managed by your secrets manager.
- Cleartext payload never appears in `audit_log` (snapshot listener strips it).

> Notes: A common audit ask is "where do you store our SendGrid API key?" Answer: encrypted at rest, never logged, decrypted only at use. Key rotation = bump `key_version` + re-encrypt; no downtime.

---

# Approval guardrails

Three layers, each independently configurable per tenant:

1. **Threshold-based approval** (E07-S03)
   `tenant_approval_settings.auto_approve_below_score`. Any content with `compliance_score < threshold` requires human sign-off.

2. **Compliance keyword scanner** (E06-S08)
   `compliance_rule` table — admin-defined patterns (`literal` / `regex` / `fuzzy`) with severity. Blocker-severity hits move asset to `failed` before approval.

3. **CAN-SPAM + unsubscribe injection** (E16-S04)
   Every email send auto-adds the tenant postal address + signed unsubscribe URL. Configurable `tenant_compliance_settings.postal_address`.

> Notes: This is the slide regulated-industry buyers will pause on. Walk the three layers: threshold → keyword scanner → CAN-SPAM. Note that all three are auditable: `audit_log` captures the score, the violations, and the final approval decision.

---

# Access control

| Role        | Read                  | Write                                                                          |
|-------------|-----------------------|--------------------------------------------------------------------------------|
| `viewer`    | All campaigns + KPIs  | —                                                                              |
| `marketer`  | + content + analytics | Create campaigns, draft content, accept recommendations                       |
| `manager`   | + audit               | Approve content, stop A/B tests, dispute reconciliation, force compliance pass |
| `admin`     | + credentials surface | Integrations, OIDC config, dismiss anomalies, run reconciliation, rotate keys |

- Enforced at the FastAPI dependency layer: `Depends(require_role(UserRole.manager))`
- Role is read from the OIDC session; never trusted from the request body
- `audit_log.actor_id` records every privileged action

> Notes: This is straightforward RBAC; the deeper point is **the role gate is in one place** (`app.api.deps.require_role`). New endpoints inherit it; you can grep for `require_role` and audit the entire authorization surface.

---

# Authentication

- **Production**: OIDC against Google Workspace by default; any OIDC IdP supported (Okta, Auth0, Azure AD).
- **Dev**: an `oidc-mock` container for local development. Tests use FastAPI's `dependency_overrides` to inject a synthetic user.
- **Sessions**: signed cookies via `SessionMiddleware` (key: `SESSION_SECRET`).
- **Tenant scoping**: derived from the OIDC `hd` claim or an explicit `tenant.oidc_hosted_domain` mapping (migration 0002).

**SCIM provisioning** is on the roadmap (Q4).

> Notes: Most enterprise buyers want SSO. We're SSO-first by default; SCIM is the gap to call out.

---

# Tool layer + LLM safety

LLM-backed agents (Strategist, Content Creator) call **registered tools**:

```python
class CopywritingTool(Tool):
    name: ClassVar[str] = "copywriting.generate"
    input_schema: ClassVar[dict] = {...}
    async def call(self, inputs): ...

tool_registry.register(CopywritingTool(...))
```

- The Anthropic client is constructed at app boot; **per-tenant rate limits** sit in front of it (`provider_rate_limit`).
- Tools are stateless and contract-tested in isolation (respx-mocked).
- Costs are bounded by `max_tokens` + `temperature` defaults; configurable per tool.
- If `ANTHROPIC_API_KEY` is unset, LLM tools simply don't register — the app boots in a degraded-but-safe mode.

> Notes: Reviewers worry about "the LLM going rogue." Two answers: (1) we don't put it in autonomous loops — every output is human-reviewable, (2) per-tenant rate limits cap blast radius.

---

# Distribution + idempotency

Email dispatch via `dispatch_attempt` row per (asset × recipient):

```sql
UNIQUE (tenant_id, idempotency_key)
```

- `idempotency_key = asset_id:recipient_email` → retries hit the conflict and read the prior status instead of re-sending.
- 24-hour recency window catches the safety-net case (`_recent_sent_recipients`).
- Frequency caps (`frequency_cap_setting`) enforced per channel.
- Suppression list (`suppression_entry`) for bounces, complaints, manual blocks.

Social dispatch follows the same shape, keyed by `asset:{aid}:channel:{cid}`.

> Notes: Walking through the SendGrid scenario: worker dies mid-batch → retries → the duplicate insert hits UNIQUE → handler reads the existing row's status → no double-send. Same model applies to LinkedIn / X / Meta.

---

# Webhook receivers

Single uniform receiver at `POST /webhooks/{provider}/{tenant_id}` (W33):

- ProviderHandler ABC + registry — drop a class in to add a provider
- Signature verification per provider (shared-secret today; provider-specific signing schemes are extensible)
- Every webhook lands in `raw_webhook` regardless of signature validity — full audit trail
- Successful events map to `analytic_event` rows with dedup on `(tenant_id, provider_event_id)`
- Failed signatures write `audit_log.action = webhook_signature_failed`
- Unmapped webhooks surface in `GET /api/webhooks/unmapped` (admin)

> Notes: Reviewers ask: "What happens to a payload that doesn't verify?" Answer: it lands, gets tagged with `signature_valid=false`, the audit row fires, and we return 401. Operator can review without losing the data.

---

# Analytics + observability

**KPI rollup** (W34): per-campaign queries against `analytic_event` with channel/asset filters. Attribution chain handles direct (Plausible UTM) and indirect (SendGrid → `dispatch_attempt.provider_message_id` → `content_asset.campaign_id`) paths.

**Anomaly detection** (W37): 14-day rolling median + 3σ deviation. Critical metrics (`unsubscribe`, `bounce`, `spam_complaint`) trigger notifications + optional auto-pause.

**OpenTelemetry tracing**: every agent → tool → DB span is traced. `init_observability` wires the FastAPI middleware + the Anthropic client + SQLAlchemy.

> Notes: Show the observability stack as a flat list, not a diagram. Buyers want to know "can my SRE see what MAS is doing?" — answer is yes, via standard OTEL collectors.

---

# Infrastructure

**Application:** FastAPI (uvicorn), Python 3.12, single binary container
**Database:** Postgres 15 (managed RDS / Cloud SQL / on-prem)
**Worker:** durable task queue inside the same Python image, leased via `task.leased_until + worker_id` (W5)
**Auth:** OIDC provider of your choice
**Optional:** Redis for ad-hoc caching; OTEL collector → your tracing backend

```
       ┌──────────────────────────────────────────────┐
       │   Load balancer (TLS termination)             │
       └─────────────────┬───────────────────────────┘
                         │
        ┌────────────────▼──────────────────┐
        │  MAS app pods (N)  + worker pods  │
        └─────────────┬─────────────────────┘
                      │
                      ▼
            ┌──────────────────────┐
            │  Postgres 15 (RLS)   │
            └──────────────────────┘
```

**Deployment options:**
- Hosted SaaS (multi-tenant) — coming
- Single-tenant on your VPC — available today via docker compose / Helm
- Air-gapped on-prem — Q4 roadmap

> Notes: The slide is purposely simple — same shape as any FastAPI + Postgres deployment. The interesting part is the worker is in-process, not a separate Celery cluster; for single-tenant deployments this halves the moving parts.

---

# Test harness

746 tests across 22 migrations, all running on every commit:

- **Unit:** state machine, tools, evaluators (pure-Python, no DB)
- **Integration:** every API endpoint + RBAC matrix; runs against a real Postgres in a testcontainer
- **Contract:** every external provider mocked via `respx` — SendGrid, LinkedIn, X, Meta, Anthropic, Plausible, HubSpot
- **End-to-end:** `scripts/full_demo.py` walks one campaign through all 6 agents in a fresh testcontainer

**CI:** Postgres testcontainer boot ~3s; full suite ~50s.

> Notes: 746 tests is the proof of "this isn't a prototype." For reviewers who care: every story in the backlog has acceptance criteria + a test that exercises them. The CI run is fast because we don't mock at the boundary — we run real Postgres and mock HTTP.

---

# What's *not* in MAS

Honest scope-setting:

- ❌ Full Google Ads / Meta Ads campaign upsert — only OAuth + account list (scaffolded)
- ❌ PDF export of the end-of-campaign report — CSV ships; PDF is a polish unit
- ❌ Charts in the UI — server-rendered tables today; chart lib choice is a polish unit
- ❌ Real-time auto-pause on anomaly — opt-in setting exists but no cron yet
- ❌ Composite KPI formulas ("X within 7d of Y") — single-event KPIs only today
- ❌ Instagram publishing — Facebook Pages only on the Meta connector

Each item is **a connector or polish unit**, not an architecture change.

> Notes: This slide builds trust. Reviewers who poke around the code will find these gaps anyway — better to surface them. The point is that each gap is small + bounded.

---

# Roadmap

| Quarter | Focus                                                                  |
|---------|------------------------------------------------------------------------|
| Q3 2026 | Ad-platform campaign upsert (Google + Meta); UI chart layer; SCIM     |
| Q4 2026 | On-prem deployment kit; LLM-backed optimisation rules; Instagram      |
| Q1 2027 | Multi-region active-passive failover; per-tenant data-residency keys  |

> Notes: Keep this thin and dated. Don't promise things that aren't real.

---

# Security review checklist

| Concern                       | Where it's addressed                                                                |
|-------------------------------|-------------------------------------------------------------------------------------|
| Cross-tenant data leakage     | Postgres RLS on every domain table + `mas_app` role with no schema privileges       |
| Credential at rest            | Fernet encryption, key rotation via `key_version`                                   |
| Credential in logs            | Audit listener strips encrypted blobs; webhook headers redact `x-mas-webhook-secret` |
| Unauthorized API access       | OIDC + `require_role` dependency; tenant scoping from session                       |
| LLM hallucination             | Human approval gate above threshold; compliance scanner pre-send                    |
| Webhook spoofing              | Signature verification per provider; raw row stored on failure for forensic        |
| Dispatch double-send          | `dispatch_attempt` UNIQUE on `(tenant, idempotency_key)`; 24h recency window        |
| Auto-action without consent   | Anomaly auto-pause + recommendation apply are explicit opt-ins per tenant           |
| Audit completeness            | Append-only `audit_log` + `agent_log`; UPDATE/DELETE revoked at the role level     |
| Multi-tenant rate limiting    | `provider_rate_limit` per (tenant, provider)                                        |

> Notes: This is the slide your security team will screenshot. Walk it once, then hand them this and the technical deck. Each row maps to a file in the codebase.

---

# Thank you

**Talk to us:** [hello@mas.example](mailto:hello@mas.example)

**Resources:**
- One-pager — [link]
- Executive deck — [link]
- Demo video (60s) — [link]
- Backend code walkthrough video (15min) — coming

> Notes: Close on a single CTA. Resources should mirror what you've already shared in the room.
