# E09 — A/B Testing

**Diagram reference:** `ab_tests` table
**Priority:** Must (MVP)
**Dependencies:** E06 (assets), E08 (dispatch), E10 (analytics)

Set up, run, and conclude A/B tests on content assets. Traffic split is consistent per recipient, significance is computed against a primary metric, and winners are promoted with an explicit decision.

---

### E09-S01 — Define an A/B test on an asset

**As a** marketer,
**I want** to attach an A/B test to an asset family,
**So that** I can compare angles or CTAs without standing it up by hand.

Priority: Must
Dependencies: E06

Acceptance criteria:
- Given an asset has variants generated (E06-S05), when I open "New A/B test", then I can select variants, the primary metric, the traffic split, and minimum runtime.
- Given I save the test, when stored, then `ab_test` is in `designing` with `variant_a_id` / `variant_b_id` set and `primary_metric` non-null.
- Given a campaign has more than one A/B test on the same asset family, when I save, then the second is rejected with a clear error.
- Given the test references an asset still in `pending_approval`, when launch is attempted, then it is blocked until the asset is `approved`.

### E09-S02 — Consistent traffic split per recipient

**As a** marketer,
**I want** the same recipient to always see the same variant,
**So that** measurement is not contaminated by re-exposure to a different arm.

Priority: Must
Dependencies: E08-S05

Acceptance criteria:
- Given a recipient is assigned an arm, when dispatch runs, then the assignment is deterministic from `(audience_member_id, ab_test_id)` and persists across retries.
- Given the recipient is in a later step of the same campaign, when sent, then the assignment is preserved.
- Given the audience is refreshed mid-test, when new members appear, then they receive a fresh random assignment per the split.
- Given the test is multivariate (>2 arms), when assigned, then the split sums to 100% within ±1%.

### E09-S03 — Significance computation

**As a** marketer,
**I want** the system to tell me when a result is significant,
**So that** I do not declare winners on noise.

Priority: Must
Dependencies: E10

Acceptance criteria:
- Given a test is `running`, when `analytic_event` rows arrive, then significance is recomputed at most every 15 minutes per test.
- Given the test reaches the configured confidence (default 95%) on the primary metric, when computed, then `status='significant'` and `winner_id` is set.
- Given the test runs for the maximum duration without reaching significance, when timeout hits, then `status='inconclusive'`.
- Given a test is stopped early via manual action, when stopped, then status becomes `stopped` and no winner is auto-set even if numerically ahead.

### E09-S04 — Holdout for measurement

**As a** marketing manager,
**I want** to hold out a portion of the audience entirely,
**So that** I can measure lift attributable to the campaign vs no-touch.

Priority: Should
Dependencies: E04-S03

Acceptance criteria:
- Given I configure a holdout (default 0%, max 30%), when audience materialises, then the held-out members are excluded from every send but tracked for measurement.
- Given the test concludes, when the report runs, then the test results are augmented with treated-vs-holdout deltas on the primary metric.
- Given the holdout is below the size needed for statistical power, when configured, then a warning shows with the minimum size to reach power.
- Given the holdout is enabled, when audit_log writes, then the holdout policy is recorded so a downstream auditor can reproduce.

### E09-S05 — Promote the winner

**As a** marketer,
**I want** the winning variant to be promoted to the rest of the campaign,
**So that** the winning content carries the remainder of the spend / traffic.

Priority: Must
Dependencies: E09-S03

Acceptance criteria:
- Given a winner is set, when I click "Promote", then the losing variant moves to `archived` and the winner's traffic share is increased to 100% for the remaining schedule.
- Given there is no scheduled remainder (test ran for the full campaign), when promoted, then only the winner is recorded for archival; no future sends occur.
- Given a manager reverses the promotion, when reverted, then the original split resumes and a clear `audit_log` reason is recorded.
- Given the winner had compliance flags (E06-S08), when promoted, then a manager-level approval is required to proceed.

### E09-S06 — A/B test history and reproducibility

**As a** marketing manager,
**I want** every A/B test's setup, traffic, and result preserved,
**So that** we learn over time and can defend decisions.

Priority: Should
Dependencies: E09-S03, E15

Acceptance criteria:
- Given a test concludes, when stored, then a snapshot is written with arms, split, audience size, daily event counts, significance trajectory, and final winner.
- Given the snapshot is queried, when read, then it is reproducible (the same inputs produce the same significance result).
- Given I view the history across campaigns, when I filter by metric, then I can compare results across tests on the same metric.
- Given the schema for `ab_test` evolves, when migrations run, then historical snapshots remain readable via a versioned reader.
