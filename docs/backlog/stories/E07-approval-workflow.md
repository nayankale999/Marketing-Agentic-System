# E07 — Approval Workflow

**Diagram reference:** Approval Orchestrator (agent map)
**Priority:** Must (MVP)
**Dependencies:** E06 (assets)

The hard gate between drafted content and any outbound action. No asset reaches the Channel Distribution agent without an `approved` decision logged for it.

---

### E07-S01 — Review queue for pending assets

**As a** marketing manager,
**I want** a single queue of everything awaiting my approval,
**So that** I do not have to navigate to each campaign in turn.

Priority: Must
Dependencies: E06

Acceptance criteria:
- Given assets exist in `pending_approval`, when I open the queue, then I see them ordered by campaign deadline, then by submitted_at ascending.
- Given I filter the queue, when applied, then I can scope by campaign, channel, asset type, and submitter.
- Given an asset has been waiting > 24h, when displayed, then it is flagged with an "overdue" badge.
- Given I lack approver permission, when I open the queue, then I see only assets where I am explicitly an approver.

### E07-S02 — Single asset approval with optional edits

**As a** marketing manager,
**I want** to approve, approve-with-edits, or reject an asset,
**So that** small fixes don't bounce back through full regeneration.

Priority: Must
Dependencies: E07-S01

Acceptance criteria:
- Given an asset is open, when I click "Approve", then a row is written to `approval_decision_log` with my id and `decision='approved'`, and the asset moves to `approved`.
- Given I edit copy then click "Approve with edits", when saved, then the edits are stored as a diff in `edits` and the final approved content is the edited version.
- Given I click "Reject", when I submit, then I am required to give a reason (free text + category) and the asset moves to `rejected`.
- Given a rejection is submitted, when the orchestrator picks up, then a regenerate task is enqueued with the reason attached.

### E07-S03 — Batch approval for related assets

**As a** marketing manager,
**I want** to approve multiple low-risk assets in one action,
**So that** routine variants do not eat my morning.

Priority: Must
Dependencies: E07-S02

Acceptance criteria:
- Given I select multiple assets in the queue, when I click "Batch approve", then I am shown a summary (count by channel, total spend exposed) and a single confirmation.
- Given I confirm, when processing runs, then each asset gets its own decision row (atomicity is per-asset, not per-batch).
- Given any asset in the batch has a compliance flag, when processing, then it is excluded from auto-approval and surfaced individually.
- Given the batch contains assets above the tenant's "auto-approval spend cap" (default zero), when attempted, then those assets are excluded and shown.

### E07-S04 — Approval thresholds by role and spend

**As an** admin,
**I want** to require higher-role approval above a spend threshold,
**So that** a single marketer cannot launch a five-figure campaign without oversight.

Priority: Must
Dependencies: E14

Acceptance criteria:
- Given an admin configures thresholds, when in place, then assets attached to campaigns above the threshold require an `admin` (not `manager`) decision.
- Given a marketer attempts to approve an asset above their level, when submitted, then it is rejected with "requires higher role".
- Given thresholds change, when applied, then in-flight approvals continue under the threshold at submission time (not the new value).
- Given an `audit_log` is written, when read, then it captures the threshold that applied at decision time.

### E07-S05 — Rejection patterns surfaced to the agent

**As a** marketing manager,
**I want** the system to surface recurring rejection patterns,
**So that** the Content Creator can be tuned rather than corrected one draft at a time.

Priority: Should
Dependencies: E07-S02, E15

Acceptance criteria:
- Given >= 20 rejections in 30 days, when the analytics agent runs, then it clusters rejection reasons and surfaces top 3 patterns to admins.
- Given a pattern reaches actionable confidence, when surfaced, then admins see a suggested brand-voice or compliance rule update.
- Given an admin applies the suggestion, when saved, then it is captured as an `audit_log` and the Content Creator's prompt is updated from the next task onward.
- Given a pattern is dismissed, when chosen, then it is hidden from the dashboard for 30 days.
