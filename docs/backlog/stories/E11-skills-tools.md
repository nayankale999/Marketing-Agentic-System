# E11 — Skills & Tools Layer

**Diagram reference:** Skills & Tools row of `MAS.png`
**Priority:** Must (MVP)
**Dependencies:** E02 (orchestration)

The six shared skills are first-class Claude Agent SDK tools, registered once and reused across agents. Each tool has versioned input/output schemas, observable latency, and the same retry semantics as the orchestrator.

---

### E11-S01 — `seo.analysis` tool

**As a** Content Creator agent,
**I want** to call an SEO analyser with a draft and target keywords,
**So that** I can score and revise before submitting for approval.

Priority: Must
Dependencies: E02

Acceptance criteria:
- Given the tool is invoked with `{ draft, target_keywords[], locale }`, when it runs, then it returns `{ score, keyword_density{}, title_quality, meta_description, recommendations[] }` within 5s p95.
- Given the draft is too short to score reliably, when called, then it returns `score=null` with `reason='insufficient_length'`.
- Given the tool is invoked from any agent, when called, then the same input produces the same output (deterministic).
- Given the tool fails, when raised, then the failure is captured in `agent_log` with the request shape and the calling agent retries per E02-S03.

### E11-S02 — `copywriting.generate` tool

**As a** Content Creator agent,
**I want** a single copywriting tool that handles per-channel constraints,
**So that** I do not write channel-specific prompts inline.

Priority: Must
Dependencies: E02

Acceptance criteria:
- Given the tool is invoked with `{ channel, asset_type, brief, voice, audience_summary, length_constraints }`, when it runs, then it returns `{ subject?, preheader?, headline?, body, cta, length_metrics }`.
- Given the channel is `email`, when called, then subject and preheader are non-null in the response.
- Given length constraints are exceeded, when generated, then the tool retries internally up to 2 times before returning the best attempt with a `length_warning`.
- Given the same brief is regenerated with a `seed`, when called twice with the same seed, then outputs are identical.

### E11-S03 — `ab.testing` tool

**As an** Analytics agent,
**I want** a tool that computes test significance,
**So that** the math lives in one place, not in agent prompts.

Priority: Must
Dependencies: E02

Acceptance criteria:
- Given the tool is invoked with `{ arm_a: { n, x }, arm_b: { n, x }, metric_kind, confidence }`, when it runs, then it returns `{ p_value, lift, confidence_interval, decision }`.
- Given the metric is a rate (binomial), when called, then a two-proportion z-test is used; for continuous metrics, Welch's t-test.
- Given the sample is too small, when called, then it returns `decision='inconclusive'` with the minimum N to reach power.
- Given the tool is called repeatedly with the same inputs, when run, then outputs match exactly across calls.

### E11-S04 — `segmentation.build` and `segmentation.estimate` tools

**As an** Audience Targeting agent,
**I want** segmentation primitives,
**So that** estimation and materialisation use the same engine.

Priority: Must
Dependencies: E01

Acceptance criteria:
- Given `segmentation.estimate` is invoked with `{ criteria, tenant_id }`, when it runs, then it returns `{ total_reachable, suppressed, net }` within 5s p95 for tenants under 1M contacts.
- Given `segmentation.build` is invoked, when it runs, then it returns an iterable of `external_id` matches that the orchestrator persists into `audience_member`.
- Given the criteria reference unsupported fields, when called, then both tools return a structured "field unavailable" error rather than empty results.
- Given an admin queries the tool's schema, when fetched, then they receive the JSON schema for both input and output (versioned).

### E11-S05 — `social.publish` tool

**As a** Channel Distribution agent,
**I want** one tool surface for social platforms,
**So that** adding a new platform is config, not new agent code.

Priority: Must
Dependencies: E12-S03

Acceptance criteria:
- Given the tool is invoked with `{ platform, channel_id, content, scheduled_at?, idempotency_key }`, when it runs, then it returns `{ provider_post_id, url, status }`.
- Given the platform's API errors with a retryable code, when called, then the tool itself retries up to 2 times with jittered backoff before surfacing.
- Given the same `idempotency_key` is submitted twice, when called, then the second call returns the original `provider_post_id` without a duplicate post.
- Given a media attachment is required, when missing, then the tool returns a precondition error before calling the platform.

### E11-S06 — `email.dispatch` tool

**As a** Channel Distribution agent,
**I want** a single email dispatch surface,
**So that** provider swap (SendGrid -> SES) is a config change.

Priority: Must
Dependencies: E12-S02

Acceptance criteria:
- Given the tool is invoked with `{ provider, audience_batch, message, idempotency_key }`, when it runs, then it returns `{ batch_id, accepted_count, rejected_count, per_message_ids[] }`.
- Given the provider is unreachable, when called, then the tool fails fast with a provider-tagged error so the orchestrator can retry against the same provider.
- Given a message contains a forbidden header or address pattern, when called, then it is rejected client-side before reaching the provider, with a clear error.
- Given suppression-suppressed recipients are passed in error, when called, then the tool drops them and reports them in `rejected_count` with reason `suppressed`.
