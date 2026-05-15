# Epic Register

Every epic maps to one or more elements of the architecture (`MAS.png`) or schema (`DBSchema.png`). Story files live under `stories/`.

## MVP Epics

| ID | Epic | Diagram ref | Priority | Stories | Depends on | Summary |
|----|------|------------|----------|---------|------------|---------|
| E01 | Data Ingestion | P1 (Data Ingestion) | Must | 6 | E12, E14 | CRM sync, market research import, campaign brief intake, web analytics ingestion with provenance |
| E02 | Orchestration Platform | P2 (Orchestration & Routing) | Must | 7 | — | Marketing Orchestrator agent, durable task queue, state machine, retries, idempotency |
| E03 | Campaign Management | `campaigns` table | Must | 6 | E01, E02 | Campaign CRUD, lifecycle states, budget allocation, brief authoring |
| E04 | Audience Targeting Agent | P4, `audiences` | Must | 6 | E01, E02 | Segmentation, ICP matching, exclusions, suppression-aware audience materialisation |
| E05 | Campaign Strategist Agent | Agent: plan | Must | 6 | E03, E04 | Strategy synthesis (channel mix, budget split, KPI targets), seasonality, brief expansion |
| E06 | Content Creator Agent | P3, `content_assets` | Must | 8 | E05, E11 | Copywriting, SEO, multi-format asset generation, variant generation, brand voice |
| E07 | Approval Workflow | Approval gate | Must | 5 | E06 | Content review queue, edit-then-approve, rejection reasons, batch approval |
| E08 | Channel Distribution Agent | P6, agent: distribute | Must | 7 | E07, E12 | Multi-channel dispatch, scheduling, throttling, suppression check, idempotent send |
| E09 | A/B Testing | `ab_tests` | Must | 6 | E06, E08 | Variant assignment, traffic split, significance, winner promotion, holdout |
| E10 | Analytics & Optimisation Agent | P5, `analytic_events` | Must | 7 | E08, E15 | Performance reports, optimisation recommendations, budget rebalancing proposals |
| E11 | Skills & Tools Layer | Skills row | Must | 6 | E02 | SEO, copywriting, A/B testing, segmentation, social, email — registered as SDK tools |
| E12 | External Integrations | External sources | Must | 6 | E14 | CRM, email provider, social/ad platforms, web analytics, webhook receivers |
| E13 | Admin & Approval UI | — | Must | 6 | E03, E07, E10 | Campaign dashboard, content review queue, reports, settings |
| E14 | Authentication & RBAC | — | Must | 5 | — | Tenant isolation, OIDC/SSO, role assignment, API keys |
| E15 | Audit & Logging | `agent_logs` | Must | 5 | E02 | Agent run logs, decision traces, append-only audit log, retention |
| E16 | NFR, Security & Compliance | — | Must | 7 | E14, E15 | SLOs, encryption, GDPR, CAN-SPAM, unsubscribe, secrets, accessibility |

**MVP totals:** 16 epics, ~100 stories.

## Phase 2 Epics (outline only)

| ID | Epic | Diagram ref | Stories | Summary |
|----|------|------------|---------|---------|
| P2-OPT | Continuous Optimisation | Feedback Loop | 5 | Auto budget reallocation, predictive uplift, auto-approval thresholds for safe variants |
| P2-PERS | Personalisation at Scale | P3 + P4 cross | 5 | Per-segment dynamic content rendering, sequence personalisation, holdout-safe rollout |

## Dependency graph (text form)

```
E14 (Auth/RBAC) ─┬─► E02 (Orchestration) ─► E11 (Tools) ─► E06 ─► E07 ─► E08 ─► E10
                 │           │                              ▲       ▲       ▲
                 │           ├─► E03 ─► E04 ─► E05 ─────────┘       │       │
                 │           │                                       │       │
                 ├─► E12 (Integrations) ──────────────────────────────────────┘
                 │           │
                 │           └─► E01 (Ingestion) ─► E03 / E04
                 │
                 ├─► E15 (Audit) ◄─── listens to all
                 │
                 └─► E13 (UI) ── consumes E03 / E07 / E10

E09 (A/B) depends on E06 (assets) + E08 (dispatch) + E10 (analytics)
E16 (NFR) cross-cuts every epic.

P2-OPT depends on E10 + 30 days of live data.
P2-PERS depends on E04 + E06 + E10.
```

## Suggested MVP delivery sequence

Five delivery slices that each end in a demonstrable capability:

1. **Foundation** — E14, E15, E02, E11 (tool registry skeleton), E16 (RBAC + secrets baseline). *Exit:* admins can configure tenants, orchestrator runs a no-op task end-to-end, audit log captures every state change.
2. **Know your audience** — E12 (CRM + email), E01, E04, E03. *Exit:* a marketer can create a campaign brief, ingest a CRM list, and build a segment with size estimate.
3. **Plan and create** — E05, E06, E11 (full SEO/copy/segmentation tools). *Exit:* the Campaign Strategist produces a channel mix + budget, the Content Creator drafts the required assets, both saved to `content_asset` + `audit_log`.
4. **Approve and launch** — E07, E08, E13 (UI for approval + launch). *Exit:* a manager approves drafts, the Channel Distribution agent sends a controlled batch on email + one social channel, suppression list is honoured.
5. **Close the loop** — E09, E10, E13 (reports dashboard). *Exit:* analytics events flow in, A/B significance is computed, the Analytics & Optimisation agent emits at least one optimisation recommendation, dashboards show activity → conversion.

## Diagram Coverage Matrix

Every element in `MAS.png` and table in `DBSchema.png` must map to ≥1 epic. Below is the forward trace.

### MAS.png — Agents

| Element | Covered by |
|---------|-----------|
| Marketing Orchestrator Agent | E02 |
| Campaign Strategist Agent | E05 |
| Content Creator Agent | E06 |
| Audience Targeting Agent | E04 |
| Analytics & Optimization Agent | E10 |
| Channel Distribution Agent | E08 |

### MAS.png — Skills / Tools

| Element | Covered by |
|---------|-----------|
| SEO Analysis | E11-S01 |
| Copywriting | E11-S02 |
| A/B Testing | E11-S03, E09 |
| Segmentation | E11-S04 |
| Social Media API | E11-S05, E12 |
| Email Automation | E11-S06, E12 |

### MAS.png — Data Sources & Outputs

| Element | Covered by |
|---------|-----------|
| Customer Data (CRM) | E12-S01, E01-S01 |
| Market Research | E01-S03 |
| Campaign Briefs | E03-S01 |
| Analytics Data | E12-S05, E01-S04 |
| Marketing Campaigns (output) | E03, E08 |
| Content Assets (output) | E06 |
| Performance Reports (output) | E10-S04, E13-S04 |
| Optimised Ad Spend (output) | E10-S05, P2-OPT |

### DBSchema.png — Tables

| Table | Covered by |
|-------|-----------|
| `agents` | E02, schema.sql |
| `campaigns` | E03, schema.sql |
| `channels` | E12, schema.sql |
| `tasks` | E02, schema.sql |
| `content_assets` | E06, schema.sql |
| `audiences` | E04, schema.sql |
| `agent_logs` | E15, schema.sql |
| `ab_tests` | E09, schema.sql |
| `analytic_events` | E10, schema.sql |

### Data Flow Processes

| Process | Covered by |
|---------|-----------|
| P1 Data Ingestion | E01 |
| P2 Orchestration & Routing | E02 |
| P3 Content Generation | E06 |
| P4 Audience Analysis | E04 |
| P5 Campaign Optimisation | E10 |
| P6 Channel Distribution | E08 |

### NFRs (cross-cutting)

| Concern | Covered by |
|---------|-----------|
| Multi-tenancy | E14, schema.sql |
| RBAC | E14 |
| Encryption at rest / in transit | E16-S01 |
| GDPR (DSAR, right-to-erasure) | E16-S03 |
| CAN-SPAM (unsubscribe, identity) | E16-S04 |
| Suppression list | E08-S04, schema.sql |
| Observability (OTel traces) | E16-S05 |
| Performance SLOs | E16-S06 |
| Accessibility (WCAG 2.1 AA) | E16-S07 |
