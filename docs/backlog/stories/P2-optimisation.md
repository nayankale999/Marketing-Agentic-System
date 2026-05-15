# P2-OPT — Continuous Optimisation (outline)

**Status:** Phase 2 — shape-only. Not ready to build until MVP is in production with 30+ days of live data.
**Diagram reference:** Feedback Loop in `MAS.png`
**Depends on:** E10 (analytics + recommendations), E09 (A/B), E08 (dispatch with idempotency)

Move optimisation from "recommend → human applies" (MVP) to "auto-apply within guardrails, surface what was applied" (Phase 2). The MVP foundation already writes `optimisation_recommendation`; this epic adds auto-application, predictive priors, and safety rails.

---

### P2-OPT-S01 — Auto-apply budget shifts within guardrails

Marketing manager configures per-tenant guardrails (max shift per day, min spend floor per channel, forbidden moves). When a recommendation falls within the rails AND clears a confidence threshold, the system applies it and posts a notification rather than waiting for approval.

Open questions:
- Default guardrails: ±15% per day? Per week?
- Should the marketer be able to roll back the last auto-apply with a single action?
- How is the rollback budget accounted for in spend reconciliation (E10-S06)?

### P2-OPT-S02 — Predictive uplift model

Train a per-tenant model on `analytic_event` history to predict the uplift of a proposed change before applying it. The model lives behind a tool (`optimisation.predict_uplift`) and is called by the Analytics & Optimisation agent.

Open questions:
- Single global model or per-tenant fine-tune?
- What's the minimum data threshold to switch from heuristics to model output?
- Where does the model live (in-process, batch, or hosted)?

### P2-OPT-S03 — Auto-approval thresholds for variant swaps

When an A/B test has converged on a winner with confidence > 99% AND uplift > 10%, the system promotes the winner automatically rather than waiting for human approval. Bounded by "auto-promote allowed" per campaign.

Open questions:
- Is auto-promotion limited to in-campaign promotion, or cross-campaign as well?
- How do we surface what was auto-promoted in the daily digest?

### P2-OPT-S04 — Scheduled creative refresh

Detect creative fatigue (declining CTR over rolling window) and trigger a Content Creator task to generate a refreshed variant automatically. Variant enters the normal approval queue unless auto-approval is on (P2-OPT-S03).

Open questions:
- Fatigue threshold: 20% CTR decline over 7 days?
- Should the original variant remain in rotation as control?

### P2-OPT-S05 — Optimisation history audit and reversal

Every auto-applied change writes to `optimisation_recommendation` with `applied_by='system'` and to `audit_log`. A "reverse last N optimisations" action exists for managers to roll back a problematic run.

Open questions:
- Reversal time horizon (last 24h? last week?).
- Does reversal restore the prior state of the campaign, or just the budget allocations?
