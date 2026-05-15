# E06 — Content Creator Agent

**Diagram reference:** P3 (Content Generation), Content Creator agent, `content_assets` table
**Priority:** Must (MVP)
**Dependencies:** E05 (strategy), E11 (tools)

Generate every required asset (email, social post, ad creative, landing copy, blog) per the approved strategy. Voice is tenant-defined; SEO and brand checks run before drafts reach the approval queue. This epic is where the marketing volume comes from.

---

### E06-S01 — Generate the assets the strategy requires

**As a** marketer,
**I want** the agent to draft every asset listed in the strategy,
**So that** I do not have to author touch-by-touch.

Priority: Must
Dependencies: E05, E11-S02

Acceptance criteria:
- Given the campaign moves to `content_in_production`, when the orchestrator picks up, then one task per required asset is enqueued and reflected in the asset list.
- Given a task runs, when complete, then a `content_asset` row exists with `status='drafted'`, channel-appropriate length, and a `metadata` payload (storage_uri, seo, brand_check).
- Given a task fails (model error, tool failure), when retries are exhausted, then the asset stays `requested` with the error captured in `agent_log` and a "regenerate" action available.
- Given all required assets reach `drafted`, when written, then the campaign moves to `approval_pending`.

### E06-S02 — Brand voice and tone configuration

**As a** marketing manager,
**I want** to capture the tenant's voice once,
**So that** every draft reads as us, not as the model's default.

Priority: Must
Dependencies: —

Acceptance criteria:
- Given I open "Brand voice", when I configure, then I can set tone descriptors, do/don't word lists, sample paragraphs, and reading-grade target.
- Given drafts are generated, when written, then the agent's system prompt includes the active voice configuration verbatim.
- Given voice is updated, when in-flight drafts are still `requested | generating`, then they use the new voice; `drafted` assets are not retroactively rewritten unless explicitly regenerated.
- Given a draft fails the do/don't word check, when validated, then it is marked `drafted` but flagged with the failing words listed in `metadata.brand_check`.

### E06-S03 — SEO check on long-form content

**As a** marketer,
**I want** SEO feedback baked into the draft step,
**So that** blog and landing copy don't need a second tool pass.

Priority: Must
Dependencies: E11-S01

Acceptance criteria:
- Given an asset is `blog_post` or `landing_page_copy`, when generated, then the SEO tool runs against target keywords and writes scores into `metadata.seo`.
- Given the SEO score is below threshold (default 60/100), when written, then the asset is flagged "needs SEO review" but is still `drafted` (not blocked).
- Given target keywords are missing on the brief, when an SEO-relevant asset is requested, then the agent prompts for keywords before generating.
- Given SEO scores are visible, when I view the asset, then I see keyword density, title quality, meta description fitness, and an actionable recommendation.

### E06-S04 — Channel-appropriate format and length

**As a** marketer,
**I want** drafts to fit the channel's constraints,
**So that** I don't see 500-character LinkedIn drafts or 4,000-character emails.

Priority: Must
Dependencies: E06-S01

Acceptance criteria:
- Given the channel is `x`, when an asset is generated, then character count <= 280 (or thread parts split correctly).
- Given the channel is `email`, when generated, then output includes subject line, preheader, body, and CTA blocks as distinct fields in `metadata`.
- Given the channel is `ad_creative`, when generated, then output includes headline (max 30 chars), description (max 90 chars), and primary text (max 125 chars) for paid social.
- Given a hard length is violated, when validated, then the asset is regenerated up to 3 times before being saved as `drafted` with a `length_warning` flag.

### E06-S05 — Multi-variant generation for A/B

**As a** marketer,
**I want** the agent to produce variants when an A/B test is planned,
**So that** test setup is not a separate manual step.

Priority: Must
Dependencies: E09, E06-S01

Acceptance criteria:
- Given the strategy has flagged an A/B test for an asset, when generation runs, then exactly two `content_asset` rows are created with the same brief and different angle/CTA tags.
- Given variants are generated, when stored, then `ab_test` is created with `variant_a_id` and `variant_b_id` populated and `status='designing'`.
- Given I request additional variants, when triggered, then up to 5 total variants can be generated and the A/B test is upgraded to multivariate (status remains `designing` until launch).
- Given variants are too similar (cosine sim > 0.9 on the body), when checked, then the agent regenerates the second variant with stronger differentiation guidance.

### E06-S06 — Regenerate with feedback

**As a** marketer,
**I want** to send rejection feedback into a regenerate,
**So that** the agent learns my edits within the campaign without needing fine-tuning.

Priority: Must
Dependencies: E07

Acceptance criteria:
- Given an asset is `rejected`, when the orchestrator regenerates, then the rejection reason text is included in the agent prompt as feedback.
- Given the asset cycles through 3 rejections, when the 4th is requested, then the orchestrator escalates to the manager queue rather than regenerating again.
- Given a manager edits the brief during regenerate, when saved, then the new brief is captured in `agent_log` and used.
- Given regenerate succeeds, when stored, then both the prior draft and the new draft are preserved (history is non-destructive).

### E06-S07 — Asset preview by channel

**As a** marketer,
**I want** to preview the asset as it will appear on the destination channel,
**So that** I catch issues before approval.

Priority: Should
Dependencies: E06-S01

Acceptance criteria:
- Given an asset exists, when I open it, then I see a channel-specific preview (email client mock, LinkedIn post card, ad creative mockup).
- Given the asset has merge fields (e.g., `{{first_name}}`), when previewed, then I can swap sample values and the preview updates live.
- Given dynamic fields fail to resolve for any audience member, when previewed against the audience, then the count of unresolved is shown.
- Given the preview is exported, when I click "Share preview", then a signed, time-bounded URL is generated and recorded in `audit_log`.

### E06-S08 — Asset compliance pre-check

**As a** RevOps admin,
**I want** the agent to run a compliance pre-check on every draft,
**So that** approval is not the only line of defence against forbidden claims.

Priority: Must
Dependencies: E16-S04

Acceptance criteria:
- Given a draft is produced, when the compliance tool runs, then forbidden claim patterns (e.g., guarantees, medical claims for non-medical products) are detected and surfaced in `metadata.compliance`.
- Given a critical compliance issue is detected, when written, then the asset is set to `drafted` with `is_required` honoured but blocked from auto-promotion to approval until a manager clears the flag.
- Given a configured suppression keyword is detected (tenant-level list), when found, then it is highlighted in the draft and the agent is asked to rewrite without it.
- Given the compliance tool is unavailable, when the draft completes, then it is held in `generating` with a clear error rather than passing without a check.
