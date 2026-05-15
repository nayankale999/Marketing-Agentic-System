# E12 — External Integrations

**Diagram reference:** External Sources column of `MAS.png`
**Priority:** Must (MVP)
**Dependencies:** E14 (RBAC + secrets)

Connect MAS to the customer's existing stack: CRM, email provider, social platforms, ad platforms, web analytics. Credentials are tenant-scoped and encrypted at rest; every external call is observable.

---

### E12-S01 — CRM connector (Salesforce / HubSpot)

**As an** admin,
**I want** to connect our CRM with OAuth,
**So that** ingestion (E01) can read contacts and accounts.

Priority: Must
Dependencies: E14

Acceptance criteria:
- Given I open "Integrations", when I click "Connect Salesforce" or "Connect HubSpot", then I am sent through OAuth and redirected back with a token persisted in `integration_credential` (encrypted).
- Given the connection is established, when I run "Test", then the connector returns at least one contact/account record.
- Given the token expires, when used, then the connector refreshes it transparently and updates the stored credential.
- Given the connection is revoked at the provider, when next used, then the connector reports `auth_revoked` and surfaces a re-auth prompt in the UI.

### E12-S02 — Email provider connector (SendGrid / SES / Postmark)

**As an** admin,
**I want** to connect an email provider,
**So that** outbound email can dispatch through it.

Priority: Must
Dependencies: E14

Acceptance criteria:
- Given I configure provider + API key, when I save, then the credential is encrypted in `integration_credential` and a "send test" works against a verified sender.
- Given the provider returns webhook events (open, click, bounce, complaint, unsubscribe), when ingested, then they map to `analytic_event` rows with idempotency keys.
- Given an unverified sender is attempted, when send is tried, then the dispatcher errors clearly before invoking the provider.
- Given the provider has multiple sub-domains for sending, when configured, then each is selectable per campaign.

### E12-S03 — Social connectors (LinkedIn, X, Meta)

**As an** admin,
**I want** to connect social accounts/pages,
**So that** distribution can publish on our behalf.

Priority: Must
Dependencies: E14

Acceptance criteria:
- Given I OAuth into LinkedIn / X / Meta, when complete, then the credential is stored encrypted and an authorised page / account is selectable in the channel record.
- Given a connector is active, when scheduled posts run, then they appear on the platform within the configured tolerance (default 60s).
- Given a connector loses authorisation, when raised, then live campaigns using it pause distribution on that channel and notify the owner.
- Given the platform changes its API version, when the connector breaks, then it surfaces a single tenant-scoped banner with the affected campaigns.

### E12-S04 — Ad platform connectors (Google Ads, Meta Ads)

**As an** admin,
**I want** to connect ad platforms,
**So that** paid campaigns and spend reporting flow through MAS.

Priority: Should
Dependencies: E14

Acceptance criteria:
- Given OAuth into Google Ads / Meta Ads completes, when I select an ad account, then it is stored and validated against the platform.
- Given a campaign with paid channels is launched, when dispatch runs, then a campaign + ad set is upserted on the platform via the connector and the ids are recorded.
- Given the platform reports spend nightly, when ingested, then `analytic_event` rows of `event_type='spend'` are written and reconciled (E10-S06).
- Given a platform-side change (paused campaign, disapproved creative), when detected via the connector's webhook or poll, then the corresponding MAS record reflects the state.

### E12-S05 — Web analytics connector (GA4 / Plausible)

**As an** admin,
**I want** to ingest web analytics,
**So that** post-click attribution is automatic.

Priority: Must
Dependencies: E14

Acceptance criteria:
- Given I connect GA4 or Plausible with service-account / API key, when valid, then `analytic_event` rows flow in attributed to campaigns via UTM.
- Given the same event is fetched twice via overlapping windows, when ingested, then dedup keeps a single row keyed by `provider_event_id`.
- Given the connector pulls every 15 min, when there is a backlog, then it advances watermarks per-window and never replays prior windows.
- Given a session attribution model is selectable, when set, then the model is recorded on the ingested events for downstream debugging.

### E12-S06 — Webhook receivers (inbound from providers)

**As a** platform engineer,
**I want** a uniform webhook receiver,
**So that** every provider's callbacks are validated, logged, and routed consistently.

Priority: Must
Dependencies: E14

Acceptance criteria:
- Given a provider POSTs to `/webhooks/{provider}`, when received, then the signature is verified per provider, the payload is stored raw, and an `analytic_event` is produced if the payload maps to a known event.
- Given the signature fails, when checked, then the request is rejected with 401 and a counter increments for the provider.
- Given a payload schema is unknown, when received, then it is stored raw under `raw_webhook` and surfaced in the admin "unmapped events" view.
- Given the receiver is rate-limited by load, when 429s out, then the provider's documented retry / replay path is honoured and idempotency keys prevent duplicates.
