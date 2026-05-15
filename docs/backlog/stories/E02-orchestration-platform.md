# E02 — Orchestration Platform

**Diagram reference:** P2 (Orchestration & Routing), Marketing Orchestrator agent
**Priority:** Must (MVP)
**Dependencies:** —

The orchestrator is the traffic controller. Nothing calls a specialist agent directly from a UI click; every action is a durable task with a well-defined state, retries, and an audit trail. This epic ships the platform that every other agentic epic builds on.

---

### E02-S01 — Durable task queue with at-least-once delivery

**As a** platform engineer,
**I want** agent work to be enqueued in Postgres with at-least-once delivery,
**So that** a worker crash never silently loses a campaign step.

Priority: Must
Dependencies: —

Acceptance criteria:
- Given a task is enqueued, when the API returns, then the task row exists in `task` with `status='queued'` and a `scheduled_for` timestamp before the transaction commits.
- Given a worker pulls a task, when it crashes mid-execution, then a lease timeout (default 5 min) returns the task to `queued` for another worker.
- Given a task succeeds, when written, then `status` becomes `succeeded` and `completed_at` is set atomically with the result payload.
- Given a task is enqueued with an idempotency key, when the same key is submitted again, then the second call returns the original task id without enqueueing a duplicate.

### E02-S02 — Marketing Orchestrator agent

**As a** marketer,
**I want** the orchestrator to route campaign work to the right specialist agent in the right order,
**So that** I never have to wire up agent calls manually.

Priority: Must
Dependencies: E02-S01, E11

Acceptance criteria:
- Given a campaign transitions to `audience_built`, when the orchestrator picks up the event, then it enqueues a Strategist task for that campaign.
- Given the Strategist completes, when the result is written, then the orchestrator enqueues Content Creator tasks for each required asset.
- Given any required upstream task fails after retries, when the orchestrator handles it, then the campaign moves to a clearly named blocked state with a human-readable reason.
- Given the orchestrator itself is restarted, when it resumes, then no in-flight campaign loses progress (state is derived from `task` rows, not in-memory).

### E02-S03 — Retry with exponential backoff and jitter

**As a** platform engineer,
**I want** transient failures retried with backoff,
**So that** rate-limits and brief outages don't kill campaign runs.

Priority: Must
Dependencies: E02-S01

Acceptance criteria:
- Given a task fails with a retryable error, when retried, then `attempt` increments and `scheduled_for` is pushed by `base * 2^attempt + jitter` (base default 30s).
- Given `attempt >= max_attempts`, when the task fails again, then status moves to `failed` and an `agent_log` row records the final error.
- Given a non-retryable error (e.g., invalid input), when raised, then the task fails immediately without consuming the retry budget.
- Given retry happens, when it is logged, then `agent_log` records both the prior attempt's error and the next scheduled time.

### E02-S04 — Idempotent agent invocations

**As a** platform engineer,
**I want** specialist agent calls to be safely retryable,
**So that** a retried task never sends a duplicate email or doubles a spend.

Priority: Must
Dependencies: E02-S03

Acceptance criteria:
- Given an agent task carries an `idempotency_key`, when re-executed after a crash, then the agent's external side-effects (writes, API calls) check the key and skip on duplicate.
- Given the same Content Creator task runs twice, when complete, then only one `content_asset` row exists for that brief.
- Given a dispatch task runs twice, when complete, then the provider receives only one send (verified via provider message id in `agent_log`).
- Given a downstream side effect cannot be made idempotent (e.g., a paid API), when called, then the task is wrapped in a serialised mutex per resource.

### E02-S05 — State machine guards and explicit transitions

**As a** product manager,
**I want** campaign state changes to be expressible as a named transition with a guard,
**So that** reviewing the lifecycle does not require reading code.

Priority: Must
Dependencies: E02-S02

Acceptance criteria:
- Given a campaign is in `drafted`, when I list the allowed transitions, then I see only the transitions enumerated in `architecture.md`.
- Given a transition has a guard (e.g., `approval_pending -> ready_to_launch` requires all required assets approved), when the guard fails, then the transition is rejected with the failing condition.
- Given a manual override role is present, when an admin forces a transition, then it is allowed but a high-severity `audit_log` entry is written with the override reason.
- Given a deprecated transition is removed, when an in-flight campaign is in a state with no successor, then the orchestrator moves it to a clearly named `requires_migration` state.

### E02-S06 — Backpressure and per-tenant rate limits

**As a** platform engineer,
**I want** one tenant's burst to not starve another,
**So that** SLA targets are achievable under concurrent load.

Priority: Should
Dependencies: E02-S01

Acceptance criteria:
- Given a tenant exceeds its concurrent-task quota, when new tasks arrive, then they are admitted to the queue with `awaiting_retry` and not dispatched to a worker.
- Given a global model-provider rate limit is hit, when reported, then all tenants share the wait fairly (no tenant monopolises the next slot).
- Given an admin queries the tenant capacity API, when called, then the response reports current usage, quota, and recent throttling events.
- Given a tenant repeatedly exceeds quota, when observed, then a notification is sent to the tenant admin (E13 inbox).

### E02-S07 — Observable run history per campaign

**As a** marketer,
**I want** to see what the agents did on my campaign,
**So that** when output surprises me I can inspect rather than guess.

Priority: Must
Dependencies: E02-S02, E15

Acceptance criteria:
- Given a campaign exists, when I open its Run History view, then I see every task in order with status, agent, duration, and a link to its `agent_log` entries.
- Given a task has tool calls, when expanded, then each tool invocation shows the inputs, outputs, and latency.
- Given a task failed, when I expand it, then the error message and the input that triggered it are visible.
- Given I lack manager role, when I view, then sensitive payloads (e.g., model prompts) are redacted but timing and status are visible.
