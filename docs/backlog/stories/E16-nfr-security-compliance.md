# E16 — NFR, Security & Compliance

**Diagram reference:** Not in `MAS.png` (cross-cutting NFR)
**Priority:** Must (MVP)
**Dependencies:** E14 (auth), E15 (audit)

The non-functional baseline: encryption, GDPR data-subject rights, CAN-SPAM compliance, secrets handling, performance SLOs, and accessibility. Stories here apply across every epic.

---

### E16-S01 — Encryption at rest and in transit

**As a** security engineer,
**I want** data encrypted in transit and at rest,
**So that** routine compromise vectors are blocked at the infra layer.

Priority: Must
Dependencies: —

Acceptance criteria:
- Given the app is deployed, when TLS is checked, then every external endpoint requires TLS 1.2+ and HSTS is set.
- Given DB and object storage are configured, when verified, then encryption-at-rest is enabled by the provider (KMS-managed keys).
- Given `integration_credential` is written, when stored, then `encrypted_payload` is the ciphertext of envelope encryption (KMS-wrapped data key); plaintext never persists.
- Given a backup is taken, when stored, then it is encrypted and access is logged.

### E16-S02 — Secrets handling

**As a** security engineer,
**I want** secrets in a dedicated store, never in source or logs,
**So that** a leaked log file does not leak production credentials.

Priority: Must
Dependencies: —

Acceptance criteria:
- Given the app starts, when secrets are needed, then they are loaded from the secret store (AWS Secrets Manager / Vault / Doppler), not env files committed to the repo.
- Given logs are emitted, when scrubbed, then matching token / key patterns are redacted by the logging middleware before write.
- Given a secret is rotated at the store, when fetched on next access, then the new value is used without a restart (TTL cache, default 5 min).
- Given a developer scans the repo, when scanned, then a pre-commit hook (gitleaks) blocks committing secrets.

### E16-S03 — GDPR data subject rights

**As a** RevOps admin,
**I want** to honour DSARs and right-to-erasure,
**So that** EU data subjects can exercise their rights.

Priority: Must
Dependencies: E15

Acceptance criteria:
- Given a DSAR request is filed for an identifier (email), when I run "Export subject", then a single bundle is produced with every record referencing that identifier across tables.
- Given a right-to-erasure request is filed, when I run "Erase subject", then PII fields are nulled in domain tables, the identifier is hashed in `audit_log` (record preserved, identifier obscured), and a deletion certificate is generated.
- Given erasure runs, when complete, then the suppression list is updated to prevent re-introduction by future ingestion.
- Given erasure conflicts with a retention hold (active investigation), when attempted, then the action is rejected with the hold reason and the requestor is informed.

### E16-S04 — CAN-SPAM and unsubscribe enforcement

**As a** RevOps admin,
**I want** every outbound channel where unsubscribe applies to enforce it,
**So that** we do not contact anyone who has opted out.

Priority: Must
Dependencies: E08

Acceptance criteria:
- Given an email send is composed, when validated, then it must contain an unsubscribe link rendering to a tenant-branded unsubscribe page and a postal address in the footer (CAN-SPAM).
- Given an unsubscribe is clicked, when processed, then a `suppression_entry` is created within 5 seconds and the unsubscribed identifier is honoured by every subsequent send.
- Given a contact is on suppression for one channel, when targeted on a different channel, then it is honoured per channel (suppression is per `channel_platform`).
- Given a one-time transactional message is needed (e.g., DSAR response), when sent, then it is exempt from suppression but tagged `transactional=true` in `audit_log`.

### E16-S05 — Observability dashboards and SLOs

**As a** platform engineer,
**I want** dashboards and alerts on SLOs,
**So that** regressions are caught before the customer reports them.

Priority: Must
Dependencies: E15-S05

Acceptance criteria:
- Given the app emits OTel traces and metrics, when reviewed, then dashboards exist for: API p95 latency, task processing latency, agent token usage, external API error rates, suppression-list freshness.
- Given an SLO is configured (e.g., API p95 < 500ms, task latency < 5 min p95), when breached for >= 5 minutes, then an alert is paged.
- Given an SLO budget is consumed > 50% in a 30-day window, when crossed, then a warning is posted to the engineering channel.
- Given an alert fires, when fielded, then runbooks linked from the alert exist for the top 5 categories.

### E16-S06 — Performance and capacity baselines

**As a** product manager,
**I want** documented capacity targets,
**So that** we can size infrastructure and quote tenants.

Priority: Must
Dependencies: E02

Acceptance criteria:
- Given the platform is benchmarked, when documented, then the README captures: max concurrent campaigns per tenant, max audience size per campaign, max dispatch rate per channel, target task throughput.
- Given a load test runs, when the campaign mix is realistic (60% email, 20% social, 20% paid), when executed at target throughput, then SLOs are met for 60 minutes without saturating any resource > 70%.
- Given a target is missed, when retested, then a follow-up story is opened explicitly.
- Given an inbound integration produces a burst (e.g., 50k webhook events in 60s), when handled, then the system absorbs without dropping (queue + worker headroom).

### E16-S07 — Accessibility (WCAG 2.1 AA)

**As a** UX engineer,
**I want** the UI to meet WCAG 2.1 AA,
**So that** marketers using assistive tech can do their work.

Priority: Should
Dependencies: E13

Acceptance criteria:
- Given the UI is built, when audited (axe-core, manual screen-reader pass on top flows), then no critical or serious violations remain on campaign detail, approval screen, and reports.
- Given keyboard-only navigation is used, when traversed, then all interactive elements are reachable and focus order is sensible.
- Given a screen reader is used, when traversed, then dynamic content (toasts, badge counts) is announced.
- Given colour contrast is checked, when measured, then it meets AA on every primary text element.
