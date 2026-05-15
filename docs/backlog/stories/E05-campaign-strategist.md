# E05 — Campaign Strategist Agent

**Diagram reference:** Campaign Strategist agent (plan)
**Priority:** Must (MVP)
**Dependencies:** E03 (campaign), E04 (audience)

Turn a brief + audience into a concrete plan: channel mix, budget allocation, KPI targets, and a sequence calendar. The Strategist proposes; the marketer approves the plan before any content is generated.

---

### E05-S01 — Generate a campaign strategy

**As a** marketer,
**I want** the Strategist agent to propose a channel mix, budget split, and timing,
**So that** I start from a defensible plan rather than a blank page.

Priority: Must
Dependencies: E03, E04, E11-S04

Acceptance criteria:
- Given a campaign with brief + audience materialised, when I click "Generate strategy", then a Strategist task is enqueued and a proposal returns within 60s p95.
- Given the proposal returns, when displayed, then I see channel mix (% per active channel), proposed budget per channel, KPI primary + secondary, and a rationale per choice.
- Given the proposal completes, when stored, then the campaign moves to `strategy_set` and the proposal is persisted as a versioned record.
- Given the campaign is missing required inputs (no audience, no objective), when I trigger, then I see a precondition error before the task is enqueued.

### E05-S02 — Editable strategy with re-plan

**As a** marketer,
**I want** to edit the proposed strategy and ask for a re-plan,
**So that** I can steer the agent without throwing out its useful parts.

Priority: Must
Dependencies: E05-S01

Acceptance criteria:
- Given a proposal exists, when I edit a field (e.g., raise email budget), then the change is captured as a "human override" tag on the affected row.
- Given I click "Re-plan with my edits", when re-run, then the agent treats my overrides as constraints and proposes around them.
- Given I revert an override, when saved, then the agent is free to propose freely on that field again.
- Given a re-plan produces a strategy materially different from the prior (>30% budget shift in any channel), when shown, then I see a diff view highlighting the change.

### E05-S03 — Multi-channel sequence calendar

**As a** marketer,
**I want** to see a calendar of touches across channels,
**So that** I can spot conflicts and frequency issues before content is generated.

Priority: Must
Dependencies: E05-S01

Acceptance criteria:
- Given a strategy exists, when I open the calendar, then I see every planned send/post across channels on a timeline from start_date to end_date.
- Given two touches land on the same audience within the frequency cap (default 3 in 7 days), when displayed, then they are flagged in amber.
- Given I drag a touch to a new date, when saved, then the change is reflected and the strategy proposal version is incremented.
- Given the campaign moves to `strategy_set`, when downstream agents pick up, then they read from the saved calendar, not a re-derived one.

### E05-S04 — Seasonality and historical priors

**As a** marketer,
**I want** the Strategist to factor in seasonality and historical campaign performance,
**So that** the plan is grounded in our data, not generic best practice.

Priority: Should
Dependencies: E10 (analytics in the warehouse)

Acceptance criteria:
- Given the tenant has at least 90 days of `analytic_event` history, when the agent runs, then it pulls baseline CTR / CVR per channel and uses them as priors.
- Given a campaign of the same `campaign_type` ran in the prior year, when the agent runs, then it cites the prior campaign in the rationale.
- Given the tenant has no history, when the agent runs, then it falls back to documented defaults and labels the proposal "no-history baseline".
- Given priors disagree by more than 2x (e.g., LinkedIn vastly outperformed email last year), when shown, then the rationale calls out the gap and the agent leans accordingly.

### E05-S05 — Constraints (must-include / must-exclude channels, hard caps)

**As a** marketing manager,
**I want** to set tenant-level constraints,
**So that** strategies cannot violate compliance or brand rules.

Priority: Must
Dependencies: E05-S01

Acceptance criteria:
- Given a tenant constraint exists (e.g., "no SMS for EU contacts"), when the Strategist proposes, then it never includes the forbidden channel for the matching audience.
- Given a hard daily/weekly send cap exists, when proposed, then the calendar respects it; exceeding caps is a validation error.
- Given a constraint changes after a strategy is set, when the campaign is still pre-launch, then the strategy is flagged out-of-date with the affected rows.
- Given I lack admin role, when I try to edit constraints, then the action is denied.

### E05-S06 — Strategy versioning and rollback

**As a** marketer,
**I want** every strategy proposal kept as a version,
**So that** I can compare and roll back if a re-plan made things worse.

Priority: Should
Dependencies: E05-S01

Acceptance criteria:
- Given multiple proposals exist, when I open "History", then I see each version with timestamp, author (agent / user), and a summary of what changed.
- Given I select two versions, when I click "Compare", then I see a side-by-side diff of channel mix, budget, and calendar.
- Given I click "Restore" on an older version, when confirmed, then the strategy is restored as a new version (not destructive) and an `audit_log` row is written.
- Given the campaign has progressed past `strategy_set`, when I restore, then the orchestrator triggers the affected downstream work to re-run.
