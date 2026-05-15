# E04 — Audience Targeting Agent

**Diagram reference:** P4 (Audience Analysis), Audience Targeting agent, `audiences` table
**Priority:** Must (MVP)
**Dependencies:** E01 (ingestion), E02 (orchestration)

Translate a marketer's ICP and exclusion intent into a materialised, suppression-aware audience snapshot. Produces both an `audience` row (criteria + estimate) and `audience_member` rows (the actual list at refresh time).

---

### E04-S01 — Define an audience with structured criteria

**As a** marketer,
**I want** to compose include/exclude rules with the fields available on contacts,
**So that** I can target without writing SQL.

Priority: Must
Dependencies: E03

Acceptance criteria:
- Given a campaign exists, when I open "Build audience", then I see a rule builder with AND/OR groups, available fields (industry, company size, role, country, recency, score, tags), and operators per field type.
- Given I save a valid rule set, when stored, then `segment_criteria` is written in the documented JSON shape and an `estimated_size` task is enqueued.
- Given a rule references a field the tenant has not ingested, when validated, then I see "field unavailable" with the source needed.
- Given two audiences exist on the same campaign with identical criteria, when I save the second, then I am warned and offered to merge.

### E04-S02 — Estimate audience size before commit

**As a** marketer,
**I want** to see the estimated audience size before committing,
**So that** I catch typos that would over- or under-target.

Priority: Must
Dependencies: E04-S01

Acceptance criteria:
- Given criteria are defined, when I click "Estimate", then the Audience Targeting agent runs `segmentation.estimate` and returns within 5s p95 for tenants under 1M contacts.
- Given the estimate completes, when shown, then I see total reachable, suppressed (excluded), and net (reachable - suppressed).
- Given the estimate is stale (criteria changed since last estimate), when displayed, then a "recompute" prompt is shown.
- Given the estimate falls outside guardrails (e.g., < 10 or > 80% of base list), when shown, then I see a non-blocking warning explaining the risk.

### E04-S03 — Materialise the audience snapshot

**As a** marketer,
**I want** the audience to be materialised at strategy-time,
**So that** late changes to contact data do not silently drift the targeting.

Priority: Must
Dependencies: E04-S02

Acceptance criteria:
- Given the campaign moves to `audience_built`, when materialisation runs, then `audience_member` rows are inserted with `external_id` and a payload snapshot per contact.
- Given a contact is on the suppression list (E16), when materialised, then it is included with a `suppressed=true` flag, not silently dropped (so the count reconciles with the estimate).
- Given materialisation completes, when finished, then `actual_size` and `refreshed_at` are updated on the `audience` row.
- Given the source data changes mid-campaign, when a refresh is triggered, then a new snapshot row-set is written under the same `audience_id` and old rows are archived rather than overwritten.

### E04-S04 — Exclusion lists and overlap detection

**As a** marketer,
**I want** to exclude contacts who are in another live campaign of mine,
**So that** the same person does not receive overlapping outreach.

Priority: Must
Dependencies: E04-S01

Acceptance criteria:
- Given I tick "Exclude live-campaign members", when criteria are evaluated, then anyone currently in another `live` campaign for my tenant is excluded.
- Given two campaigns target overlapping audiences, when I view either, then I see the count of overlap with a link to the other campaign.
- Given exclusions cause my audience to fall below the size threshold, when I save, then I see the impact before commit and can choose to proceed.
- Given the orchestrator picks up the audience for content generation, when exclusions changed since the audience was built, then the audience is recomputed before content generation begins.

### E04-S05 — ICP and persona templates

**As a** marketing manager,
**I want** to save audience definitions as reusable ICPs,
**So that** the team uses the same definition across campaigns.

Priority: Should
Dependencies: E04-S01

Acceptance criteria:
- Given I have a saved audience, when I "Save as ICP", then a tenant-level template is created with my edits applied as default.
- Given a campaign is created from an ICP template, when I open the audience editor, then the ICP rules are pre-filled and editable.
- Given an ICP is updated, when I view dependent campaigns, then I see whether the new definition would shrink or grow each audience.
- Given an ICP is deleted, when campaigns reference it, then they are decoupled (their criteria remain) and warned at edit time.

### E04-S06 — Audience explainability

**As a** marketer,
**I want** to see why a specific contact was included or excluded,
**So that** I can debug surprising audience sizes without paging engineering.

Priority: Should
Dependencies: E04-S03

Acceptance criteria:
- Given a contact exists in my tenant, when I look up "Why this contact?" for a given audience, then I see which rule branches matched and which excluded.
- Given a contact was excluded by the suppression list, when shown, then I see the reason (unsubscribed / bounced / complaint) and date.
- Given I query the API for an audience member, when the response returns, then it includes a `match_explanation` JSON.
- Given I lack PII visibility, when explaining, then identifiers are masked but rule matches are visible.
