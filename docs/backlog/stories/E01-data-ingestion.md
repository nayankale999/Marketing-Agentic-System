# E01 — Data Ingestion

**Diagram reference:** P1 (Data Ingestion), inputs row of `MAS.png`
**Priority:** Must (MVP)
**Dependencies:** E12 (integrations), E14 (RBAC)

Bring source data into MAS — CRM lists, market research, campaign briefs, web/ad analytics — with validation, provenance, and freshness. This is the on-ramp; every downstream epic reads from the tables this epic populates.

---

### E01-S01 — Sync contacts and accounts from CRM

**As a** marketer,
**I want** to pull contacts and accounts from our CRM on demand or on a schedule,
**So that** my audiences are built on current data, not stale exports.

Priority: Must
Dependencies: E12-S01 (CRM connector)

Acceptance criteria:
- Given an admin has configured a CRM integration, when I trigger a sync, then contact + account records appear in a staging area within the tenant and a `task` row tracks the run.
- Given a record already exists from a prior sync, when re-synced, then fields update in place and `audit_log` records the diff (no duplicate row).
- Given the CRM returns an error mid-sync, when the job stops, then partial progress is preserved, the task is marked `failed`, and the operator can resume from the last successful cursor.
- Given a record is missing required identifiers (no email, no CRM id), when validation runs, then it is quarantined with a reason rather than inserted.

### E01-S02 — Upload audience seed via CSV

**As a** marketer,
**I want** to upload a CSV of contacts as a one-off audience seed,
**So that** I can run a campaign against a list I assembled outside the CRM.

Priority: Must
Dependencies: E01-S01

Acceptance criteria:
- Given I open "Upload audience", when I download the template, then I receive a CSV with the documented header row (email, first_name, last_name, company, country, tags).
- Given I upload a file up to 50,000 rows, when processing begins, then I see a progress indicator and a `task` row in the queue.
- Given the file processes, when complete, then I receive a summary (imported / skipped / failed) with a downloadable error report keyed by row number and reason.
- Given a row's email is already on the suppression list (E16), when validation runs, then it is imported as suppressed and excluded from outreach by default.

### E01-S03 — Import market research notes

**As a** marketer,
**I want** to attach market-research notes (PDFs, links, free text) to a campaign,
**So that** the Strategist agent has the context I would brief a human team with.

Priority: Should
Dependencies: E03-S01

Acceptance criteria:
- Given a campaign exists, when I attach a PDF or paste notes, then the document is stored in object storage with a content hash and a row in `campaign_research`.
- Given I attach a URL, when fetch succeeds, then the page text is extracted and stored alongside the URL with a fetched-at timestamp.
- Given the document is over 25 MB or the URL fetch fails, when I submit, then I see a clear error and no partial record is created.
- Given a research item is attached, when the Strategist agent runs (E05), then it can read the item via the `research_lookup` tool.

### E01-S04 — Ingest web analytics events

**As a** marketer,
**I want** web analytics events to flow into MAS,
**So that** post-launch performance is attributable per campaign and channel.

Priority: Must
Dependencies: E12-S05 (web analytics connector)

Acceptance criteria:
- Given the GA4 / Plausible integration is configured, when the connector polls, then `analytic_event` rows are written with `event_type`, `metric_value`, and the originating `campaign_id` (resolved via UTM).
- Given an event arrives with a UTM that does not match any campaign, when written, then it is stored with `campaign_id = NULL` and surfaced in an "unattributed" report.
- Given the connector is rate-limited, when polling fails, then it backs off exponentially and resumes from the last watermark on next run.
- Given duplicate events arrive within the dedup window, when ingested, then only the first is retained (idempotency key: `provider_event_id`).

### E01-S05 — Provenance and freshness on every record

**As a** RevOps admin,
**I want** every ingested record to carry its source and last-refreshed time,
**So that** I can spot stale data before campaigns are built on it.

Priority: Must
Dependencies: E01-S01

Acceptance criteria:
- Given a record is ingested, when written, then `source` (crm | csv | research | analytics) and `fetched_at` are populated and non-null.
- Given a record has not been refreshed within the configured TTL (default 30 days), when audiences are built, then it is flagged "stale" in the audience composition view.
- Given an admin views a record, when they open its history, then they see every refresh with diff and timestamp.
- Given I query the API for a record, when I include `?include=provenance`, then the response carries the same fields.

### E01-S06 — Operator dashboard for ingest jobs

**As a** RevOps admin,
**I want** a single view of all running and recent ingest jobs,
**So that** I can diagnose stuck syncs without paging engineering.

Priority: Should
Dependencies: E01-S01, E13

Acceptance criteria:
- Given I open the Ingest Jobs page, when it loads, then I see all jobs from the last 7 days with status, source, row counts, and duration.
- Given a job is `failed`, when I click it, then I see the error message, the last good cursor, and a "retry from cursor" action.
- Given a job is `running`, when I view it, then I see progress as `processed / total` with an ETA.
- Given I lack the `ops` role, when I open the page, then I see only my own jobs.
