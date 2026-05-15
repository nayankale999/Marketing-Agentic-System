# MAS — MVP Build Plan

**Audience:** one engineer (you) pairing with Claude Code.
**Stack:** Python 3.12, FastAPI, Claude Agent SDK, PostgreSQL 15+, Alembic, OpenTelemetry, pytest.
**Source of truth:** [docs/backlog/](backlog/) — every work unit below names the story ID(s) it implements.

The plan is structured as five **delivery slices** (matching `epics.md` § "Suggested MVP delivery sequence"). Each slice is broken into **work units (W*)** that are roughly 0.5–2 days of solo work and end in something you can demo. Slice 1 is fully decomposed; Slices 2–5 are one-page outlines until we're ready to start them.

**Solo execution principles:**
- One work unit in flight at a time. No parallel branches.
- Each work unit ends with an explicit demo / passing test, not "looks good".
- Pairing rule: I (Claude Code) write the first cut, you review and steer before the next file lands.
- Definition-of-done for a story = its ACs pass in an integration test, not a unit test alone.

---

## Slice 1 — Foundation (deep plan)

**Epics covered:** E14 (Auth/RBAC), E15 (Audit), E02 (Orchestration), E11 (Tools skeleton), E16 (NFR baseline: secrets, OTel).
**Slice exit:** an empty "campaign event" flows through the orchestrator end-to-end with auth, tenant isolation, audit log, retries, and a trace captured. No business logic yet — the whole foundation is provably working before any specialist agent lands.

### W1 — Repo scaffold

**Goal:** the dev loop works.
**Touches:** repo root, `app/`, `tests/`, CI.

Tasks:
- `pyproject.toml` with `uv` (preferred) or `poetry`. Pinned: fastapi, pydantic v2, sqlalchemy 2.x, alembic, anthropic, claude-agent-sdk, pytest, pytest-asyncio, httpx, ruff, mypy.
- `app/` tree: `api/`, `agents/`, `db/`, `tools/`, `integrations/`, `settings/`, `observability/`, `__main__.py`.
- `tests/` tree mirroring `app/`.
- Pre-commit: ruff (format + lint), mypy, gitleaks.
- `Makefile` or `justfile` with: `dev`, `test`, `migrate`, `lint`, `typecheck`.
- `docker-compose.yml` with Postgres 15 + a `mailhog` (used later) + `otel-collector`.
- `app/api/health.py` → `GET /health` returning `{status: "ok"}`.

**Exit:** `make dev` brings up Postgres and FastAPI; `curl /health` returns 200; `make test` passes one smoke test.

### W2 — Database + Alembic + schema migration

**Goal:** `docs/backlog/schema.sql` runs as the first Alembic migration.
**Implements:** schema.sql wired in.

Tasks:
- `alembic init app/db/migrations`.
- One migration file that runs `schema.sql` (or transcribes it to Alembic ops; prefer ops for downgrade support).
- SQLAlchemy models for `tenant`, `app_user`, `agent`, `campaign`, `task`, `audit_log`, `agent_log` (rest come slice-by-slice).
- `app/db/session.py`: connection pool, request-scoped session.
- Integration test fixture: ephemeral schema per test using `pytest-postgresql` or a docker container with truncate-between-tests.
- A smoke test creates a tenant + app_user via SQLAlchemy and reads them back.

**Exit:** `alembic upgrade head` on an empty DB creates every table from `schema.sql`; integration test passes; rollback (`downgrade -1`) works.

### W3 — Auth scaffolding (E14-S01, E14-S02)

**Goal:** every API call is identified and role-checked.
**Implements:** E14-S01, E14-S02.

Tasks:
- Local OIDC: use `authlib` + a mock IdP container (`oidc-mock`) for dev; design for plug-in real IdP via env.
- `app/api/auth.py`: session middleware reading the OIDC ID token, populating `request.state.user`.
- `app/api/deps.py`: `require_role(role)` FastAPI dependency.
- `app/api/me.py`: `GET /api/me` returning the current user + role.
- A test endpoint `GET /api/_protected_marketer` that requires marketer role.
- CI test: walk the FastAPI router tree, fail if any route lacks a `require_role` dependency (allowlist for `/health` and `/api/me`).

**Exit:** mock user signs in → `/api/me` returns their profile; a viewer hits the protected endpoint and gets 403; CI route-coverage test passes.

### W4 — Tenant isolation + RLS (E14-S03, E14-S04)

**Goal:** no query crosses tenants, defended in two layers.
**Implements:** E14-S03, E14-S04.

Tasks:
- Add `tenant_id` to `request.state` (derived from the user's tenant).
- In `session.py`: per-request `SET LOCAL app.tenant_id = :tid` after acquiring a connection.
- Migration: enable RLS on every domain table; policies of the form `USING (tenant_id = current_setting('app.tenant_id')::uuid)`.
- A "maintenance role" SQL role that bypasses RLS for Alembic + admin work; document and log its use.
- Cross-tenant test: create two tenants with overlapping data; assert that a session for tenant A returns zero rows from tenant B.
- A failing query test: clear `app.tenant_id` mid-test → assert zero rows / explicit error.

**Exit:** cross-tenant fuzz test passes; manual `psql` session without `SET app.tenant_id` returns zero domain rows.

### W5 — Audit + agent log writers (E15-S01, E15-S02)

**Goal:** every state change is captured, append-only.
**Implements:** E15-S01, E15-S02.

Tasks:
- `app/audit/writer.py`: `write_audit(actor, entity_kind, entity_id, action, before, after, metadata)` writing to `audit_log` in the same transaction as the change.
- `app/audit/middleware.py`: FastAPI middleware that wraps mutation endpoints with before/after capture (snapshot the row, run handler, snapshot again, write audit).
- `app/agents/log.py`: `agent_log_emit(agent_id, task_id, action, log_data)` for the orchestrator and agents to call.
- Deploy script that runs `REVOKE UPDATE, DELETE ON audit_log, agent_log FROM mas_app` (the app role).
- Test: create a campaign via API → `audit_log` has an `entity_kind='campaign', action='created'` row with `after_state` populated.
- Test: SQLAlchemy attempt to UPDATE `audit_log` from the app role raises.

**Exit:** every mutation in the test suite produces a matching `audit_log` row; the append-only constraint test passes.

### W6 — Durable task queue (E02-S01, E02-S03)

**Goal:** at-least-once task delivery with retries.
**Implements:** E02-S01, E02-S03, E02-S04 (idempotency keys).

Tasks:
- `app/orchestrator/queue.py`: `enqueue_task(...)`, `claim_next(worker_id, lease_seconds)`, `complete(task_id, output)`, `fail(task_id, error)`.
- `SELECT ... FOR UPDATE SKIP LOCKED` based claim with a lease column.
- `app/orchestrator/worker.py`: a process that loops, claims, dispatches to a registered handler dict, handles retry/backoff.
- Idempotency: `task.idempotency_key` is unique; `enqueue_task` is upsert-on-key.
- A no-op `echo` agent that returns its input.
- Tests: (a) enqueue → worker completes → status succeeded; (b) crash mid-execution (kill worker after claim) → lease expires → another worker picks up and completes; (c) failing handler → exponential backoff with jitter visible in `scheduled_for`.

**Exit:** ~50 echo tasks complete from CLI; crash-recovery test passes; retry budget exhausts to `failed`.

### W7 — Marketing Orchestrator agent + state machine (E02-S02, E02-S05)

**Goal:** state transitions on `campaign` route work to the right agent.
**Implements:** E02-S02, E02-S05.

Tasks:
- `app/orchestrator/state_machine.py`: declarative `Transition(from_state, to_state, guard, on_enter)` table for campaigns; matches `architecture.md` § 3.
- `app/orchestrator/router.py`: subscribes to "campaign state changed" events; for each state, knows which task(s) to enqueue next. For Slice 1, only one transition is wired: `drafted -> drafted` re-enters an echo task (placeholder until E03/E04/E05 land).
- API: `POST /api/campaigns/{id}/transitions/{name}` invokes the SM.
- Test: drive a campaign through `drafted -> drafted` 5 times; assert each transition writes `audit_log` and enqueues one echo task that the worker consumes.

**Exit:** state machine accepts only the documented transitions; one round trip is observable end-to-end in the worker logs.

### W8 — Tool registry skeleton (E11)

**Goal:** specialist agents will register tools the same way; build the harness first.
**Implements:** E11 skeleton (one happy-path tool, one always-fails tool, no real logic).

Tasks:
- `app/tools/base.py`: `Tool` base class wrapping Claude Agent SDK tool registration with input/output JSON Schema; auto-logs every invocation to `agent_log`.
- `app/tools/_stubs.py`: `echo_tool` (success) and `flaky_tool` (fails first N times, succeeds after).
- A test agent (`app/agents/_test_agent.py`) that uses both stub tools through the SDK and runs in the orchestrator.
- Test: agent run produces `agent_log` rows for each tool invocation with latency + outcome; `flaky_tool` retries per the orchestrator retry budget.

**Exit:** stub tool happy path + retry path both visible in `agent_log`; tool registration auto-discovery works.

### W9 — Secrets, settings, observability baseline (E16-S01, E16-S02, E16-S05)

**Goal:** prod-grade configuration and traces without leaving the lab.
**Implements:** E16-S01 (TLS via reverse proxy in compose), E16-S02 (secret store interface), E16-S05 (OTel).

Tasks:
- `app/settings.py` with `pydantic-settings` reading from env + a `.env.local`.
- `app/secrets/`: `SecretStore` interface; `EnvSecretStore` (dev), stub `KmsSecretStore` (prod, ENV-feature-flagged).
- `app/observability/`: OTel SDK init; trace middleware tags every span with `tenant_id`, `user_id`, `request_id`; metrics exporter; structured logging with regex-based redaction for `ghp_*`, `sk_*`, `xoxb-*`, `Bearer .*`.
- Local OTel collector in docker-compose; Grafana / Jaeger optional.
- pre-commit: `gitleaks` confirmed.
- Test: a sample API call produces a trace span visible in the collector; a log line containing `ghp_TESTLEAKTOKEN1234` is redacted before write.

**Exit:** a full request (sign in → create campaign → enqueue task → worker echoes) shows one connected trace; secrets never appear in logs.

### Slice 1 demo (end-to-end)

After W9, you should be able to:
1. `make dev` → stack up.
2. Sign in as a mock OIDC user (admin role).
3. `POST /api/tenants` → tenant created, `audit_log` row written.
4. `POST /api/campaigns` → campaign created in `drafted`, audit captured.
5. `POST /api/campaigns/{id}/transitions/drafted` (self-loop placeholder) → orchestrator enqueues echo task → worker consumes → `agent_log` row written.
6. Inspect OTel: see the span tree spanning API → orchestrator → worker → agent → tool.
7. Cross-tenant test: a second tenant's session sees none of this.

If all six work, you have the platform. Every business epic from Slice 2 onward lands on top of it.

---

## Slice 2 — Know your audience (outline)

**Epics:** E12 (CRM + email connectors only), E01 (ingestion), E04 (audience), E03 (campaign).
**Exit:** a marketer creates a campaign brief, ingests a CRM contact list, builds a segment with size estimate, materialises the audience snapshot.

High-level work units:
- **W10** — Campaign CRUD + brief authoring (E03-S01, E03-S02, E03-S06).
- **W11** — CRM connector: Salesforce OR HubSpot first, the other later (E12-S01).
- **W12** — CSV upload + validation + dedup (E01-S02, E01-S03).
- **W13** — Provenance + freshness + ingest dashboard (E01-S05, E01-S06).
- **W14** — `segmentation.estimate` + `segmentation.build` tools (E11-S04).
- **W15** — Audience Targeting agent + materialisation + exclusion (E04-S01–E04-S04).
- **W16** — Web analytics connector + UTM attribution (E12-S05, E01-S04).

**Slice 2 demo:** OAuth into Salesforce, sync 1k contacts, define an audience ("US + Engineering + last interacted in 30 days"), see size estimate, materialise.

---

## Slice 3 — Plan and create (outline)

**Epics:** E05 (Strategist), E06 (Content Creator), E11 (full SEO + copywriting tools).
**Exit:** the Strategist agent proposes a channel mix and budget; the Content Creator drafts the required assets; both saved with audit trail.

High-level work units:
- **W17** — `copywriting.generate` tool with channel constraints (E11-S02).
- **W18** — `seo.analysis` tool (E11-S01).
- **W19** — Brand voice configuration (E06-S02).
- **W20** — Campaign Strategist agent: channel mix + budget + KPI proposal (E05-S01, E05-S02, E05-S05).
- **W21** — Sequence calendar (E05-S03).
- **W22** — Content Creator agent: generate required assets per strategy (E06-S01, E06-S04).
- **W23** — Compliance pre-check + variant generation (E06-S05, E06-S08).
- **W24** — Asset preview by channel (E06-S07).

**Slice 3 demo:** existing audience → Strategist proposes a plan → I edit + approve plan → Content Creator drafts 6 assets across 3 channels → all in `drafted` state with SEO scores + brand checks.

---

## Slice 4 — Approve and launch (outline)

**Epics:** E07 (Approval), E08 (Distribution), E13 (UI for approval + launch), E12 (social channel #1).
**Exit:** a manager approves drafts, the Channel Distribution agent sends a controlled batch on email + one social channel, suppression list is honoured.

High-level work units:
- **W25** — Approval review queue + single asset decision (E07-S01, E07-S02).
- **W26** — Batch approval + thresholds (E07-S03, E07-S04).
- **W27** — Email provider connector (E12-S02) + `email.dispatch` tool (E11-S06).
- **W28** — Schedule + dispatch (E08-S01, E08-S02, E08-S05).
- **W29** — Suppression list + frequency capping enforced at send (E08-S04, E16-S04).
- **W30** — Social connector #1 (LinkedIn) + `social.publish` tool (E12-S03, E11-S05).
- **W31** — Manual emergency stop + throttling (E08-S06, E08-S07).
- **W32** — Minimal UI: campaign detail + approval review screen (E13-S02, E13-S03).
- **W33** — Webhook receivers for delivery / unsubscribe events (E12-S06).

**Slice 4 demo:** approve a drafted email + LinkedIn post → scheduled → sent to a small test audience → bounce/unsubscribe webhook flows in → suppression list updates → next send respects it.

---

## Slice 5 — Close the loop (outline)

**Epics:** E09 (A/B), E10 (Analytics & Optimisation), E13 (reports), remaining E12 channels.
**Exit:** analytics events flow in, A/B significance is computed, the Analytics & Optimisation agent emits at least one recommendation, dashboards show the loop.

High-level work units:
- **W34** — Real-time KPI dashboard backend (E10-S01).
- **W35** — A/B test definition + traffic split (E09-S01, E09-S02).
- **W36** — `ab.testing` significance tool + winner promotion (E11-S03, E09-S03, E09-S05).
- **W37** — Analytics & Optimisation agent: anomaly detection + recommendations (E10-S02, E10-S03).
- **W38** — End-of-campaign report (E10-S04, E13-S04).
- **W39** — Budget rebalancing proposals (E10-S05).
- **W40** — Remaining channels: Meta / X / Google Ads / Meta Ads as appetite allows (E12-S03, E12-S04).
- **W41** — Custom KPIs + spend reconciliation (E10-S07, E10-S06).

**Slice 5 demo:** a live campaign with an A/B test running → significance reached after N events → winner promoted → end-of-campaign report rendered + exported.

---

## Cross-cutting work (interleaved through all slices)

These don't get a slice of their own — they ride along with whatever epic exercises them:

- **DSAR / right-to-erasure (E16-S03)** — first useful when CRM data lands in Slice 2.
- **OTel dashboards + SLO alerts (E16-S05, E16-S06)** — extend the Slice 1 baseline as new components arrive.
- **Accessibility audit (E16-S07)** — runs against the UI as it grows in Slice 4 and Slice 5.
- **Retention policies (E15-S04)** — finalise once `analytic_event` volume is real (mid-Slice 5).
- **API keys (E14-S05)** — implement when the first external automation needs one (probably Slice 4).
- **Notifications inbox (E13-S06)** — fold into approval UI in Slice 4.

---

## Decisions to lock before Slice 1 starts

These are deferred until you say "let's scaffold":

1. **Package manager:** `uv` (fast, modern) vs `poetry` (familiar). Default: `uv`.
2. **IdP for dev + prod:** `oidc-mock` for dev is fine; the prod IdP (Google Workspace? Microsoft? Auth0?) needs to be picked before W3.
3. **Frontend framework:** Slice 4 introduces UI. Options: Next.js (full stack TS app talking to FastAPI), HTMX + Jinja (simpler, server-rendered), Streamlit (fastest but limited). Default for solo: HTMX + Jinja for MVP, swap to Next.js if you want a richer UI later.
4. **Hosting target:** local-only until a slice exits demoable. After Slice 1, decide: Fly.io / Railway / Render (single-tenant indie) vs AWS (production aspirations).
5. **Email provider for the first integration:** SendGrid / Postmark / SES — pick before W27.

---

## How we'll work

- **Per work unit:** I write the first cut of files, run the tests, propose the next step. You review, accept or redirect. We commit at the end of each W unit with a message of the form `Wn: <short summary> (E0X-SYZ)`.
- **Per slice:** end with the demo above + a short retrospective (what surprised us, what we'd cut next time). Tag the commit `slice-N-demo`.
- **Open questions:** captured at the end of each story in `docs/backlog/stories/` when they come up. Don't carry them in your head.
- **Out of scope reminders:** Phase 2 work, mobile UI, multi-region. Anything not in E01-E16 stays in `docs/backlog/stories/P2-*.md` until MVP is in production.
