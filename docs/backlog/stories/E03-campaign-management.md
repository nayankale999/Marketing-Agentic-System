# E03 — Campaign Management

**Diagram reference:** `campaigns` table, "Marketing Campaigns" deliverable
**Priority:** Must (MVP)
**Dependencies:** E01 (ingestion), E02 (orchestration)

The container every other artifact attaches to. This epic owns campaign CRUD, lifecycle transitions, budget bookkeeping, and the brief that downstream agents read.

---

### E03-S01 — Author a campaign brief

**As a** marketer,
**I want** a structured form for capturing the brief,
**So that** the Strategist agent has the same inputs a human team would receive.

Priority: Must
Dependencies: E02

Acceptance criteria:
- Given I open "New Campaign", when I fill the form, then I see required fields (name, type, objective, start_date, end_date, budget_total, currency, KPI primary + target) and optional fields (notes, hero offer, competitors, exclusions).
- Given I submit valid input, when the request succeeds, then a `campaign` row is created with `status='drafted'` and an `audit_log` entry.
- Given start_date is after end_date, when validated, then the form rejects with a field-level error.
- Given budget_total is set, when stored, then it is captured in the campaign's currency and a per-channel allocation row defaults to 0 for each active channel.

### E03-S02 — Edit a campaign before launch

**As a** marketer,
**I want** to edit a campaign while it is still pre-launch,
**So that** I can iterate without recreating the record.

Priority: Must
Dependencies: E03-S01

Acceptance criteria:
- Given a campaign is in `drafted | audience_built | strategy_set | content_in_production | approval_pending | ready_to_launch`, when I edit, then the change is allowed and `audit_log` captures before/after.
- Given a campaign is `live | paused | completed`, when I attempt to edit core fields (dates, objective, type), then the edit is blocked with a clear message; budget and KPI may be edited with a manager role.
- Given an edit changes the audience criteria, when saved, then a recompute task is enqueued for the audience.
- Given an edit changes the brief, when saved, then the Strategist agent is re-invoked only if the campaign has not yet been approved.

### E03-S03 — Lifecycle transitions match architecture

**As a** product manager,
**I want** the campaign lifecycle to match `architecture.md` exactly,
**So that** UI, API, and agents agree on what state means.

Priority: Must
Dependencies: E02-S05

Acceptance criteria:
- Given the documented states (drafted, audience_built, strategy_set, content_in_production, approval_pending, ready_to_launch, live, optimising, paused, completed), when I query the campaign API, then `status` returns one of these values only.
- Given a transition is attempted that is not in the state machine, when called, then the API responds 409 with the allowed transitions for the current state.
- Given a campaign reaches `live`, when stored, then `launched_at` is set; reaching `completed` sets `completed_at`.
- Given the state machine changes, when migration runs, then in-flight rows are reconciled per the migration plan in the affected story.

### E03-S04 — Per-channel budget allocation

**As a** marketer,
**I want** to split the campaign budget across channels,
**So that** I and the Strategist agent share the same constraint.

Priority: Must
Dependencies: E03-S01

Acceptance criteria:
- Given a campaign and a set of channels, when I allocate amounts, then `sum(allocated) <= budget_total` is enforced.
- Given allocations exist, when the Strategist agent runs (E05), then it sees the user's allocation as input and may propose a revision (subject to approval).
- Given a channel has zero allocation, when the Distribution agent runs, then it does not dispatch through that channel.
- Given the campaign is `live`, when actual spend per channel is updated, then the allocation row's `spent` increases and crossing 100% emits a notification.

### E03-S05 — Clone a campaign

**As a** marketer,
**I want** to clone an existing campaign,
**So that** repeatable motions are one click instead of a form refill.

Priority: Should
Dependencies: E03-S01

Acceptance criteria:
- Given I clone a campaign, when the request completes, then a new campaign exists in `drafted` with the same brief, type, KPI targets, and channel allocations, but new dates and a `(copy)` suffix.
- Given the source had attached research items, when cloned, then references are copied (not the files themselves).
- Given the source had content assets, when cloned, then assets are NOT copied unless I explicitly opt in.
- Given the source had a live A/B test, when cloned, then the cloned test is created in `designing` with no traffic.

### E03-S06 — Campaign list with filters and search

**As a** marketer,
**I want** to filter and search across my campaigns,
**So that** I can find what I am working on without scrolling.

Priority: Must
Dependencies: E03-S01

Acceptance criteria:
- Given I open the campaign list, when it loads, then I see my tenant's campaigns ordered by `updated_at` desc.
- Given I filter by status, type, owner, or date range, when applied, then the list updates and the URL is shareable.
- Given I type into the search bar, when I submit, then matching names appear via trigram search; results return within 500ms p95 for 10k campaigns.
- Given I lack permission to view a campaign, when listing, then it does not appear in my results.
