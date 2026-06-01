# MAS — Marketing Agentic System

**A six-agent system that runs end-to-end marketing campaigns the way your team would — only with no context switching, no spreadsheets, and a full audit trail.**

---

## The problem

A mid-market marketing team running a single multi-channel campaign touches **six to ten tools** before launch — brief docs, HubSpot for the audience, a copy generator, an approval ticketing system, SendGrid, LinkedIn, Plausible, Google Sheets for spend, Slack for status. Half the week disappears into the seams.

That's expensive when one person is doing it. It's untenable when the team scales.

---

## The system

MAS is one product with six specialist agents on top of FastAPI, Postgres, and the Claude Agent SDK:

| Agent                       | What it owns                                                                 |
|-----------------------------|-------------------------------------------------------------------------------|
| **Audience Targeting**      | ICP definition, segmentation, materialisation against HubSpot / CSV uploads  |
| **Campaign Strategist**     | Channel mix, allocation, calendar, KPI targeting                              |
| **Content Creator**         | Channel-aware copy with brand voice, A/B variants, SEO check, compliance scan |
| **Approval Orchestrator**   | Threshold-based human review, rejection routes, audit                         |
| **Channel Distribution**    | Email (SendGrid), LinkedIn, X, Meta dispatch with idempotency + frequency caps |
| **Analytics & Optimisation**| Real-time KPIs, anomaly detection, A/B significance, budget rebalancing       |

Tied together by an orchestrator state machine, a durable task queue, and end-to-end OpenTelemetry tracing.

---

## What's different

- **Multi-tenant from day 1** — Postgres row-level security, encrypted per-tenant credentials, per-tenant rate limits. Onboard a new team in minutes, not a migration cycle.
- **Approval gates are first-class** — no auto-publishing without a human sign-off when content scores above the configured risk threshold. Every approval lands in `audit_log` with the diff and the manager.
- **Built-in compliance** — CAN-SPAM footer + unsubscribe URL injection on every email send. Configurable keyword-based compliance scanner blocks creative before it reaches the queue.
- **A/B testing that's actually deterministic** — recipients hash to the same variant across retries, paused/resumed campaigns, and multi-touch flows. Significance is computed with a real two-proportion z-test.
- **The agent recommends, the human accepts** — anomaly auto-pause and budget shifts are opt-in per tenant. The system never silently changes spend.
- **Open architecture** — every integration is a connector class; every tool is registered through a single registry; new channels drop in without restructuring.

---

## Proof points

- **Coverage**: 746 tests across 22 migrations. Every story in the MVP backlog (E01–E16) ships with green ACs.
- **Observability**: OpenTelemetry traces span agent → tool → DB. Every state transition writes to `audit_log`.
- **Reproducibility**: A single `python -m scripts.full_demo` boots a fresh Postgres, runs every migration, and walks a campaign brief → end-of-campaign report in under 30 seconds.

---

## Who it's for

- **B2B SaaS demand-gen teams** (Series A–C) where 2–4 marketers run multi-channel campaigns and can't afford a marketing-ops hire yet.
- **Mid-market e-commerce** with separate growth and CRM functions that need shared attribution.
- **Regulated industries** (FinServ, healthcare) where audit, approval, and compliance are non-negotiable.

---

## What you get on day 1

| Layer                     | Day 1                                                                                              |
|---------------------------|----------------------------------------------------------------------------------------------------|
| Hosted instance           | Docker compose, single-tenant, on your VPC; multi-tenant SaaS is a separate offering              |
| Integrations              | HubSpot, SendGrid, LinkedIn, Plausible all wired; X + Meta social ship live; ad platforms scaffolded |
| Auth                      | OIDC (Google Workspace, Okta, or any OIDC IdP); SCIM provisioning on the roadmap                  |
| Onboarding                | One-day implementation walkthrough; brand voice + compliance rules captured in the first session  |

---

**Talk to us:** [hello@mas.example](mailto:hello@mas.example) · Demo video: [link]
