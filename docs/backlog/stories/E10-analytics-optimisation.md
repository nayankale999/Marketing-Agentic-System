# E10 — Analytics & Optimisation Agent

**Diagram reference:** P5 (Campaign Optimisation), Analytics & Optimization agent, `analytic_events` table
**Priority:** Must (MVP)
**Dependencies:** E08 (sends produce events), E15 (event durability)

Compute performance reports, surface anomalies, and propose optimisations (budget shifts, creative swaps, schedule changes) for human approval. In MVP this agent recommends; in Phase 2 it can auto-apply within configured guardrails.

---

### E10-S01 — Real-time performance KPIs per campaign

**As a** marketer,
**I want** live KPIs while my campaign is running,
**So that** I know if something is off before the end-of-day digest.

Priority: Must
Dependencies: E08

Acceptance criteria:
- Given a campaign is `live`, when I open its dashboard, then I see impressions, opens, clicks, replies, conversions, spend, CPL/CPA, and unsubscribe rate updated within 5 minutes of ingestion.
- Given filter by channel or content asset, when applied, then the numbers recompute for that scope.
- Given a metric is below an `analytic_event` provider's reporting latency, when displayed, then the UI shows the freshness ("synced 12 min ago").
- Given two channels report the same event type at different rates, when aggregated, then the dashboard documents the latency per source.

### E10-S02 — Anomaly detection on hot metrics

**As a** marketer,
**I want** unusual movements flagged,
**So that** I don't miss problems while attention is elsewhere.

Priority: Must
Dependencies: E10-S01

Acceptance criteria:
- Given a baseline (rolling 14-day median) exists per metric per campaign, when a window deviates by > 3σ, then an anomaly is recorded.
- Given an anomaly fires on a critical metric (unsubscribe, bounce, spam complaint), when raised, then the campaign owner + manager receive a notification within 10 minutes.
- Given the deviation persists for 2 consecutive windows, when observed, then the orchestrator can auto-pause if "auto-pause on critical anomaly" is enabled (default off).
- Given an anomaly is dismissed by an admin, when chosen, then it is silenced for that metric for 24h.

### E10-S03 — Optimisation recommendations

**As a** marketer,
**I want** the agent to propose specific changes I can accept,
**So that** I don't have to interpret a dashboard.

Priority: Must
Dependencies: E10-S01

Acceptance criteria:
- Given a campaign has 7 days of data, when the optimisation agent runs (nightly), then it writes 0+ `optimisation_recommendation` rows with a `proposal` and predicted uplift.
- Given a recommendation is shown to a marketer, when displayed, then I see (a) the change, (b) the rationale, (c) the predicted impact, and (d) which historical data supports it.
- Given I accept a recommendation, when applied, then the corresponding state change is written (budget shift, schedule change, asset variant swap) and `applied_at` / `applied_by` are recorded.
- Given the predicted uplift is below a configured threshold (default 5%), when proposed, then it is hidden in the default view but available in "all recommendations".

### E10-S04 — End-of-campaign performance report

**As a** marketing manager,
**I want** a single end-of-campaign report,
**So that** I can share with stakeholders without assembling it.

Priority: Must
Dependencies: E10-S01

Acceptance criteria:
- Given a campaign moves to `completed`, when the report is generated, then it includes objectives, KPI delivery vs target, channel breakdown, A/B test outcomes, anomalies, recommendations applied / rejected, and total spend.
- Given a report is generated, when I export, then I can choose PDF or CSV; both contain the same numbers.
- Given a report has no data for any KPI, when displayed, then the relevant section says "no data" rather than zero (data absence vs zero is preserved).
- Given the report is regenerated after late-arriving data, when re-run, then both versions are preserved and the latest is the default view.

### E10-S05 — Budget rebalancing proposals

**As a** marketing manager,
**I want** the agent to surface budget shifts between channels,
**So that** we put money where it is working.

Priority: Should
Dependencies: E10-S03

Acceptance criteria:
- Given a campaign is `live` for >= 5 days, when the optimisation agent runs, then it computes effective cost-per-outcome per channel and proposes shifts > 10% if data supports it.
- Given a shift is proposed, when shown, then I see the source channel, target channel, proposed amount, and confidence.
- Given I accept the shift, when applied, then `campaign_channel_budget.allocated` updates atomically and the next dispatch wave uses the new allocation.
- Given the proposed shift would put a channel below its minimum daily spend (set by ad platform), when proposed, then it is clamped to the floor and the rationale calls this out.

### E10-S06 — Spend reconciliation

**As a** RevOps admin,
**I want** committed spend reconciled against provider invoices,
**So that** the budget bookkeeping does not drift.

Priority: Should
Dependencies: E12-S04

Acceptance criteria:
- Given an ad platform reports spend nightly, when ingested, then `campaign_channel_budget.spent` is updated to the platform's authoritative number.
- Given an invoice reconciliation runs monthly, when complete, then a reconciliation report flags campaigns where committed vs invoiced differs by > 1%.
- Given a discrepancy is flagged, when an admin reviews, then they can mark it explained (with note) or open a dispute item.
- Given a campaign is `completed` and reconciled, when read, then its spend is read-only.

### E10-S07 — Custom KPI definitions

**As a** marketing manager,
**I want** to define custom KPIs from event combinations,
**So that** "qualified lead" or "demo booked" lives next to opens and clicks.

Priority: Should
Dependencies: E10-S01

Acceptance criteria:
- Given I open "Custom KPI", when I define one, then I can express it as a formula over `event_type`, audience filter, and time window (e.g., `clicks_where(landing="/demo") within 7d of send`).
- Given a custom KPI is saved, when used on a campaign, then it appears alongside the standard KPIs with same freshness.
- Given a custom KPI references a missing event, when evaluated, then it returns null with a clear "event missing" message rather than zero.
- Given a custom KPI is deleted, when in use on a campaign, then the campaign keeps its historical numbers but the KPI is greyed out.
