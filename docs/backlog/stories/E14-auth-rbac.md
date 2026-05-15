# E14 — Authentication & RBAC

**Diagram reference:** Not in `MAS.png` (cross-cutting NFR)
**Priority:** Must (MVP)
**Dependencies:** —

Tenant isolation, SSO-friendly auth, four roles, scoped API keys. No domain epic should land before this is in place.

---

### E14-S01 — OIDC / SSO authentication

**As an** admin,
**I want** to sign in with our IdP,
**So that** our security team is not chasing one-off password resets.

Priority: Must
Dependencies: —

Acceptance criteria:
- Given the tenant configures OIDC (Google / Microsoft / generic), when a user authenticates, then a session is established with email, name, and tenant attributes.
- Given a user signs in for the first time, when no matching `app_user` exists, then JIT provisioning creates the row with role `viewer` (admin must elevate).
- Given the IdP returns a non-matching domain, when login completes, then it is rejected with a clear "domain not allowed for this tenant" message.
- Given a session expires, when an API call is attempted, then a 401 is returned with a re-auth hint.

### E14-S02 — Four roles enforced on every endpoint

**As a** RevOps admin,
**I want** four roles (admin, manager, marketer, viewer) enforced everywhere,
**So that** permission gaps are not a custom check per route.

Priority: Must
Dependencies: E14-S01

Acceptance criteria:
- Given every API endpoint declares a required role, when called by a lower role, then the call is rejected with 403 and a clear message.
- Given role decorators are missing on a new endpoint, when CI runs, then the build fails (test enforces "every route has a role").
- Given a manager is reviewing a marketer's campaign, when read-only, then they see all fields but cannot edit unless they hold the explicit transfer.
- Given a viewer attempts any state change, when called, then they are denied without revealing whether the resource exists.

### E14-S03 — Per-tenant data isolation

**As a** security engineer,
**I want** every query scoped by `tenant_id`,
**So that** no leak across tenants is structurally possible.

Priority: Must
Dependencies: —

Acceptance criteria:
- Given the data access layer requires a `tenant_id` on every query, when one is missing, then the call raises an unrecoverable error in dev and aborts the request in prod.
- Given a request crosses tenant boundaries (admin assistance scenario), when made, then it requires a privileged "impersonation" role and writes an `audit_log` row with high severity.
- Given a test asserts isolation, when run nightly, then it cross-fuzzes tenants and confirms zero reads outside scope.
- Given a row is created without a `tenant_id` set, when inserted, then the DB constraint rejects the insert.

### E14-S04 — Row-level security (RLS) on Postgres

**As a** security engineer,
**I want** RLS policies as a belt-and-braces layer,
**So that** a bug in the data layer doesn't expose another tenant's rows.

Priority: Must
Dependencies: E14-S03

Acceptance criteria:
- Given RLS is enabled on every domain table, when a session sets `app.tenant_id`, then queries return rows only for that tenant.
- Given a session sets no tenant, when a query runs, then zero rows are returned (fail closed).
- Given a migration adds a new domain table, when CI runs, then it fails if RLS is not also configured.
- Given a maintenance role is used (migrations, backfills), when explicitly elevated, then RLS bypass is logged.

### E14-S05 — Scoped API keys

**As a** developer,
**I want** scoped API keys per integration,
**So that** revoking one key does not require rotating everything.

Priority: Must
Dependencies: E14-S02

Acceptance criteria:
- Given an admin creates an API key, when issued, then I select scopes (roles + endpoint families) and an optional expiry; the key is shown once and stored as a hash.
- Given a key is used, when called, then `app_user.last_login_at` analogue (`api_key_last_used_at`) updates and rate limits apply per key.
- Given a key is revoked, when next used, then it is rejected with 401 and an `audit_log` row is written.
- Given a key has an expiry, when reached, then it stops working at the second; renewal is a discrete admin action.
