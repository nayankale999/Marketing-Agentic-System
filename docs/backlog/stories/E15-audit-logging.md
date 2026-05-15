# E15 — Audit & Logging

**Diagram reference:** `agent_logs` table, cross-cutting audit
**Priority:** Must (MVP)
**Dependencies:** E02 (orchestration emits the events)

Two streams: per-agent telemetry (`agent_log`) and system-wide append-only audit (`audit_log`). Both are write-only from the app role at deploy time. Retention is tenant-configurable.

---

### E15-S01 — Agent run telemetry

**As a** platform engineer,
**I want** every model call, tool call, and decision logged,
**So that** I can debug a campaign run without re-executing it.

Priority: Must
Dependencies: E02

Acceptance criteria:
- Given an agent task runs, when complete, then `agent_log` carries: model id, prompt token count, completion token count, latency, tool calls (name + duration), and any error.
- Given the task includes external API calls, when logged, then provider + endpoint + status are captured (request/response bodies are NOT — only hashes).
- Given a task is retried, when logged, then each attempt is its own `agent_log` row with the attempt number.
- Given a task is cancelled, when logged, then the cancellation reason is captured.

### E15-S02 — Append-only system audit log

**As a** compliance auditor,
**I want** every state change captured immutably,
**So that** I can reproduce who-did-what without trusting the app.

Priority: Must
Dependencies: —

Acceptance criteria:
- Given a domain object changes (campaign, asset, audience, ab_test, approval, integration credential), when committed, then an `audit_log` row is written with actor, before, after, action, and metadata.
- Given the app role is deployed, when checked, then `UPDATE` and `DELETE` on `audit_log` and `agent_log` are revoked (`pg_table_privileges` confirms).
- Given a maintenance/migration role is used, when audit rows are altered, then a high-severity warning is emitted and that role's actions are themselves logged externally.
- Given a row is being written from an agent (not a user), when logged, then `actor_kind='agent'` and `actor_id` is the agent uuid.

### E15-S03 — Audit export

**As a** compliance auditor,
**I want** to export audit rows for an entity or a time range,
**So that** I can deliver evidence in the requested format.

Priority: Must
Dependencies: E15-S02

Acceptance criteria:
- Given I have audit-export permission, when I query the export endpoint with `entity_kind`, `entity_id` or `since`/`until`, then I receive an NDJSON / CSV stream of the rows.
- Given an export exceeds 100k rows, when triggered, then it is delivered as a job (E13 notifications inbox) with a signed download URL.
- Given an export is generated, when stored, then the export request itself is captured in `audit_log` with the resulting file hash.
- Given exports are produced, when retention applies, then exported files inherit the tenant's retention policy.

### E15-S04 — Retention policy

**As a** RevOps admin,
**I want** to configure how long telemetry and audit are retained,
**So that** compliance and storage cost both meet our policy.

Priority: Must
Dependencies: E15-S02

Acceptance criteria:
- Given an admin sets a retention period per table (default: `agent_log` 90 days, `audit_log` 7 years, `analytic_event` 24 months), when nightly maintenance runs, then older rows are removed.
- Given a retention deletion runs, when complete, then the count and reason are themselves logged in `audit_log` (`actor_kind='system'`).
- Given an "investigation hold" is placed on an entity, when set, then no rows referencing that entity are removed regardless of retention.
- Given the policy is updated, when applied, then it takes effect on the next maintenance run, not retroactively in the current one.

### E15-S05 — OpenTelemetry trace propagation

**As a** platform engineer,
**I want** OTel traces spanning request → orchestrator → agent → tool → provider,
**So that** I can diagnose latency end-to-end.

Priority: Should
Dependencies: E02

Acceptance criteria:
- Given a request enters the API, when traced, then a root span is created with `tenant_id`, `user_id`, `request_id` attributes.
- Given the request enqueues a task, when picked up by a worker, then the trace continues across the boundary (B3 / W3C propagation in `task.metadata`).
- Given a tool call is made via the SDK, when measured, then the tool name, latency, and outcome are attributes on the span.
- Given a span exceeds a configured latency SLO, when measured, then it is sampled at 100% and surfaced in the observability backend.
