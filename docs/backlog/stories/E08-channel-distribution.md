# E08 — Channel Distribution Agent

**Diagram reference:** P6 (Channel Distribution), Channel Distribution agent
**Priority:** Must (MVP)
**Dependencies:** E07 (approval), E12 (integrations)

Take approved assets and push them to the configured channels at the planned time, with throttling, suppression honoured, and idempotent send.

---

### E08-S01 — Schedule approved assets on the calendar

**As a** marketer,
**I want** approved assets to land at the planned slot,
**So that** I do not have to babysit the calendar.

Priority: Must
Dependencies: E07

Acceptance criteria:
- Given an asset is `approved` and its calendar slot is in the future, when the orchestrator picks up, then `status` moves to `scheduled` and `scheduled_at` is set on the asset.
- Given the campaign moves to `ready_to_launch`, when all required assets are scheduled, then the campaign transitions to `live` at the start_date.
- Given an asset's slot is already in the past at approval time, when scheduling, then the marketer is warned and offered "send now" or "move to next available slot".
- Given a scheduling change is made, when saved, then `audit_log` captures the prior and new slot.

### E08-S02 — Dispatch email via provider

**As a** marketer,
**I want** email sends to go via the configured provider,
**So that** deliverability matches what our domain has earned.

Priority: Must
Dependencies: E12-S02

Acceptance criteria:
- Given an email asset is `scheduled`, when its slot arrives, then a dispatch task is created and the provider receives a send with the asset's content + audience batch.
- Given the provider returns a per-message id, when received, then it is stored on the analytic_event idempotency key for delivery / bounce reconciliation.
- Given the provider returns an error (rate limit, auth), when raised, then the dispatch task retries per E02-S03 and the asset stays `scheduled` unless permanently failed.
- Given a successful batch completes, when written, then the asset moves to `published` and per-recipient send rows are NOT stored (provider holds them); only the aggregate is logged.

### E08-S03 — Dispatch to social / paid channels

**As a** marketer,
**I want** social and paid sends to use the same dispatch pipeline,
**So that** behavior is consistent across channels.

Priority: Must
Dependencies: E12-S03, E12-S04

Acceptance criteria:
- Given a `social_post` is scheduled for a connected social channel, when dispatched, then it appears on the platform at the scheduled time and the post URL is captured on the asset.
- Given an `ad_creative` is scheduled, when dispatched, then a campaign + ad set is upserted on the ad platform and the platform's ad id is stored.
- Given a multi-channel asset is scheduled, when dispatched, then each channel's dispatch is an independent task; partial success is reflected per channel.
- Given a channel is disconnected at dispatch time, when attempted, then the task fails with `channel_unavailable` and the campaign is flagged.

### E08-S04 — Suppression and frequency capping at send time

**As a** RevOps admin,
**I want** the suppression list and frequency caps checked again at send,
**So that** late changes to those rules cannot be bypassed by stale audiences.

Priority: Must
Dependencies: E04-S03, E16-S04

Acceptance criteria:
- Given an audience was materialised earlier, when dispatch runs, then suppression is re-checked against `suppression_entry` immediately before send.
- Given a contact has received >= the frequency cap in the last N days, when send is attempted, then it is skipped and the skip is logged.
- Given a contact is on the suppression list, when send is attempted, then it is never sent regardless of audience inclusion.
- Given an entire send is skipped due to caps, when complete, then the asset is marked `published` with `delivered_count=0` and a `skip_reason` recorded.

### E08-S05 — Idempotent send (no doubles on retry)

**As a** platform engineer,
**I want** retries to never cause duplicate sends,
**So that** a flake in our pipeline does not embarrass us with the audience.

Priority: Must
Dependencies: E02-S04

Acceptance criteria:
- Given a dispatch task carries an idempotency key derived from `(content_asset_id, audience_member_id)`, when retried, then a previously-sent provider response is detected and not re-sent.
- Given the provider's message id was captured, when retry runs, then the agent reads the stored id and verifies status with the provider rather than re-sending.
- Given the provider's id was not captured (failure before capture), when retry runs, then a deterministic send-window dedup window (default 24h) prevents same-content sends to the same recipient.
- Given a contact has been sent in the last 24h for the same campaign step, when checked, then duplicates are blocked at the dispatcher.

### E08-S06 — Throttling per provider and per audience

**As a** RevOps admin,
**I want** sends throttled to respect provider and platform limits,
**So that** we don't get rate-limited or marked spammy.

Priority: Must
Dependencies: E08-S02

Acceptance criteria:
- Given a per-provider rate limit is configured, when dispatch runs, then sends are paced and reported throughput stays under the limit.
- Given a per-audience throttling policy (e.g., max 1k sends/hour), when dispatch starts, then it spreads the send window accordingly and `agent_log` records the pacing decision.
- Given an inbound provider 429, when received, then dispatch backs off and respects the provider's `Retry-After` header.
- Given the campaign has a hard daily cap, when reached, then remaining sends are pushed to the next day's window.

### E08-S07 — Manual emergency stop

**As a** marketing manager,
**I want** a single "stop everything" action on a live campaign,
**So that** when something is wrong I can halt before more damage.

Priority: Must
Dependencies: E08-S01

Acceptance criteria:
- Given a campaign is `live`, when I click "Pause campaign", then no further dispatch tasks are run for that campaign and the campaign moves to `paused`.
- Given dispatch tasks are in-flight, when pause hits, then queued tasks are cancelled and currently-executing tasks complete (sends do not stop mid-batch) but no new batch is started.
- Given a campaign is paused, when I "Resume", then the orchestrator continues from the next scheduled slot, skipping any whose slot has elapsed.
- Given a critical compliance flag triggers (E16), when raised, then the system auto-pauses the campaign without manual action and notifies the owner + manager.
