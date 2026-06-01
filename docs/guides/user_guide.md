# MAS — User Guide

For marketers, marketing managers, and viewers using the Marketing
Agentic System day-to-day.

> If you're setting up MAS for the first time, integrating tools, or
> managing users, see the **Administration Guide** instead.

---

## Contents

1. [What MAS does for you](#1-what-mas-does-for-you)
2. [Signing in](#2-signing-in)
3. [Roles — what you can do](#3-roles--what-you-can-do)
4. [The campaign lifecycle in one picture](#4-the-campaign-lifecycle-in-one-picture)
5. [Creating a campaign brief](#5-creating-a-campaign-brief)
6. [Audiences](#6-audiences)
7. [Strategy proposal — review + accept](#7-strategy-proposal--review--accept)
8. [Content drafts + brand voice](#8-content-drafts--brand-voice)
9. [Approving content](#9-approving-content)
10. [A/B tests](#10-ab-tests)
11. [Launching the campaign](#11-launching-the-campaign)
12. [Monitoring — KPIs, anomalies, recommendations](#12-monitoring--kpis-anomalies-recommendations)
13. [End-of-campaign report + CSV export](#13-end-of-campaign-report--csv-export)
14. [Custom KPIs](#14-custom-kpis)
15. [Pause / resume / complete](#15-pause--resume--complete)
16. [FAQ + troubleshooting](#16-faq--troubleshooting)

---

## 1. What MAS does for you

MAS plans, creates, distributes, and optimises marketing campaigns
through **six specialist agents** working from one shared database:

| Agent                       | What it does for you                                            |
|-----------------------------|------------------------------------------------------------------|
| **Audience Targeting**      | Builds an audience from your CRM / CSV based on a segment rule  |
| **Campaign Strategist**     | Proposes a channel mix, allocation, and calendar                 |
| **Content Creator**         | Drafts channel-aware copy in your brand voice (incl. A/B variants) |
| **Approval Orchestrator**   | Routes drafts through your approval gates                         |
| **Channel Distribution**    | Sends email + social posts with retries, frequency caps, and audit trails |
| **Analytics & Optimisation**| Tracks KPIs, flags anomalies, proposes budget shifts             |

You stay in control. **Agents recommend; humans accept.** Nothing leaves
the door without an approval (above your configured threshold), and
budget changes only land when you click "Accept" on a recommendation.

---

## 2. Signing in

In production MAS uses **OIDC** — your Google Workspace, Okta, or Azure
AD account. Visit your MAS URL and click *Sign in*; you'll be redirected
to your identity provider and back.

The first time you sign in, MAS creates an account in the `viewer`
role. Ask an admin to upgrade you to `marketer`, `manager`, or `admin`
based on what you need to do.

### Dev/staging

Local development uses a mock OIDC server. The shortcut
`/api/auth/dev-impersonate?email=you@yourdomain.test` is enabled only
when `DEV_IMPERSONATION_ENABLED=true` and 404s otherwise.

---

## 3. Roles — what you can do

| Action                                          | Viewer | Marketer | Manager | Admin |
|-------------------------------------------------|--------|----------|---------|-------|
| Read campaigns + KPIs                            | ✓      | ✓        | ✓       | ✓     |
| Create campaign briefs                           |        | ✓        | ✓       | ✓     |
| Edit audience criteria                           |        | ✓        | ✓       | ✓     |
| Accept strategy proposals                        |        | ✓        | ✓       | ✓     |
| Draft / regenerate content                       |        | ✓        | ✓       | ✓     |
| Approve content                                  |        |          | ✓       | ✓     |
| Approve compliance-flagged content               |        |          | ✓       | ✓     |
| Stop A/B test early                              |        |          | ✓       | ✓     |
| Accept optimisation recommendations              |        | ✓        | ✓       | ✓     |
| Dismiss anomalies                                |        |          |         | ✓     |
| Run spend reconciliation                         |        |          |         | ✓     |
| Configure integrations / brand voice / approvals |        |          |         | ✓     |

Higher roles satisfy lower-role requirements automatically.

---

## 4. The campaign lifecycle in one picture

```
drafted → audience_built → strategy_set → content_in_production →
approval_pending → ready_to_launch → live → optimising
                                          ↕  pause / resume
                                       paused
                                          ↓
                                      completed
```

You can monitor a campaign's state in the badge at the top of its
detail page. State transitions are automatic when their preconditions
are met (e.g. all required assets approved → `ready_to_launch`); a few
are manual (`pause`, `resume`, `complete_campaign`).

---

## 5. Creating a campaign brief

A campaign brief is the input every agent reads. Be specific.

**Required fields:**
- **Name** — short, internal label (e.g. "Q3 Manufacturing Buyer Push")
- **Campaign type** — `awareness`, `lead_gen`, `demand_gen`, `nurture`,
  `product_launch`, `event_promo`, `retention`
- **Objective** — single sentence, measurable (e.g. "Drive 100 MQLs
  from automation/manufacturing buyers in 6 weeks")
- **Brief** — 2–5 paragraphs: who you're targeting, what you're saying,
  what the supporting evidence is, what success looks like
- **Budget total** + currency
- **Start / end dates**
- **KPI targets** — at least one primary metric (e.g. `conversion = 100`)
  and optional secondaries (`click`, `open`, `reply`)

**Tip — the brief drives the Strategist.** A vague brief produces a
vague strategy proposal. The single biggest lever you have on output
quality is the brief.

After saving, the campaign is in `drafted` status.

---

## 6. Audiences

### Option A — Upload a CSV

`Audiences → Upload CSV` on the campaign detail page. Required column:
`email`. Optional: `first_name`, `last_name`, `company`, `country`,
`title`, plus any custom fields you want available in templates.

MAS deduplicates against any existing audience for the campaign.

### Option B — Materialise from HubSpot

If your admin has connected HubSpot, choose `Audiences → Build from
HubSpot`. Provide a **segment rule** — country, industry, company size,
job title, lifecycle stage, tags. MAS materialises the matching contacts
as an audience snapshot.

### Tips
- Keep your initial audience to **the people most likely to convert**.
  You can always add more later.
- The system warns you if an audience is < 10 or > 80% of your base
  list (E04-S02). Tighten the criteria.
- The **freshness** field shows when the audience was last refreshed —
  re-materialise before launch if it's older than your tenant's
  freshness TTL (default 30 days).

---

## 7. Strategy proposal — review + accept

Once the audience is built, the Strategist drafts a proposal. You'll
see it under `Strategy` on the campaign detail page. It includes:

- **Channels** — which platforms (Email, LinkedIn, X, Meta) and
  what % of your budget goes to each
- **Allocation amount** — actual currency amount per channel
- **Rationale** — why the Strategist proposed this split
- **KPIs** — primary + secondary metrics with targets
- **A/B tests** — proposed split tests (e.g. 2 subject lines on
  email touchpoint 1)
- **Calendar** — sequence of touchpoints with dates and channels

### Reviewing
- Tweak any allocation_pct or touchpoint date in the form. Save.
- The proposal version increments each time you save edits.
- Click **Accept** to lock it in. The campaign moves to
  `strategy_set`.

### Multiple proposals
You can request another proposal from the Strategist (e.g. with a
revised brief). Only one proposal can be `accepted` at a time.

### Tenant constraints
Your admin may have set guardrails like `forbid_channel` or
`hard_cap` (E05-S05). The Strategist respects these — if the proposal
looks limited, that's why.

---

## 8. Content drafts + brand voice

After you accept the strategy, the Content Creator generates one
`content_asset` per touchpoint. Each asset has:

- **Status** — `requested` → `generating` → `drafted` →
  `pending_approval` → `approved` → `scheduled` → `published`
  (or `rejected` / `failed`)
- **Channel-specific fields** — for email: subject, preheader, body.
  For LinkedIn/X/Meta: post text + optional media URL.
- **Compliance scan results** — keyword matches against your tenant's
  compliance rules
- **SEO score** — keyword density + length checks
- **Brand voice score** — how closely the draft matches your
  configured voice

### Editing
- Click the asset to edit any field inline. Saving updates the draft.
- **Regenerate** asks the Content Creator to draft again. Use this
  after a rejection or when you want a different angle.

### Brand voice
Your admin configures the brand voice (`tone`, `audience_persona`,
`forbidden_phrases`, `signature`). The Content Creator reads it on
every generation. Update the voice once — it applies forward to all
new drafts.

### What's NOT auto-generated
- The audience CSV
- Strategy acceptance
- Approval decisions
- Recommendation acceptance
- Anomaly dismissal

---

## 9. Approving content

Drafts move to `pending_approval` automatically (or when you click
**Submit for approval**). Open the approvals queue at
`/ui/approvals/queue` to see everything waiting on a human decision.

### Decision options

| Action                | Effect                                                                 |
|-----------------------|------------------------------------------------------------------------|
| **Approve**           | Asset → `approved` → eligible for scheduling                          |
| **Approve with edits**| Same, but your in-line edits are persisted to the asset content        |
| **Reject**            | Asset → `rejected`. Content Creator can regenerate.                    |

### Compliance flags
When the keyword scanner flags an asset:
- **Severity `blocker`** — the asset moves to `failed` automatically.
  You can't approve it without a manager fixing the wording or
  overriding via the manager-only force-pass action.
- **Severity `warning`** — the asset still requires approval. You'll
  see the matched keywords + severity in the review panel.

### Auto-approve threshold
Your admin can configure a `auto_approve_below_score` threshold
(E07-S03). Drafts with a compliance score below the threshold skip
the human-review queue and auto-advance to `approved`. Anything at
or above the threshold lands in your queue.

Every approval decision is recorded in `audit_log` with the diff and
the approver.

---

## 10. A/B tests

When the Strategist's proposal includes `ab_tests`, MAS pre-creates
2+ variants on the relevant touchpoint. You can also create a test
manually from the campaign detail page.

### Defining a test
- **Variants** — pick 2 to 5 approved (or scheduled) assets
- **Primary metric** — `open`, `click`, `conversion`, etc.
- **Traffic split** — integer percentages that sum to 100
- **Min runtime** — minimum hours before declaring a winner
- **Max runtime** — hours after which the test gives up and is marked
  `inconclusive`

### Launching
Click **Launch** on the test. Status moves `designing` → `running`.
MAS rejects launch if any variant is not in `approved`/`scheduled`.

### How recipients are assigned
Every recipient is hashed (Blake2b on `audience_external_id +
ab_test_id`) into a stable variant. The same recipient always sees
the same variant — across retries, multi-touch flows, and pause/resume
cycles.

### Significance + promotion
- MAS re-evaluates the test at most every 15 minutes (W36).
- When p-value < 5% (95% confidence) on the primary metric, the test
  flips to `significant` and `winner_id` is set.
- Click **Promote** to push the winner to 100% traffic. Losing variants
  flip to `archived`.
- **Compliance flag on the winner?** Only a `manager` or `admin` can
  promote — marketer attempts return a 409.

### Stopping early
Managers can stop a running test via **Stop**. The test ends in
`stopped` and no winner is auto-set even if numerically ahead.

---

## 11. Launching the campaign

Preconditions:
- Strategy proposal `accepted`
- All required content assets `approved` or `scheduled`
- At least one audience member

When the preconditions are met, the campaign auto-advances to
`ready_to_launch`. From here:
- Manual launch via the **Launch** button (manager or admin)
- Automatic at the `start_date` (when ready_to_launch + start ≤ now)

Once `live`:
- The Distribution agent schedules + dispatches each asset at its
  touchpoint slot
- Retries with backoff on provider 5xx / 429
- Suppression list (bounces, complaints, manual blocks) is honoured
  on every send
- Frequency caps prevent the same recipient from being hit too often

You can pause at any time. See [Pause / resume / complete](#15-pause--resume--complete).

---

## 12. Monitoring — KPIs, anomalies, recommendations

### KPI dashboard

Open the campaign detail page. The KPI panel updates within ~5 min of
event ingestion. Metrics shown:

| Metric                 | Where it comes from                                       |
|------------------------|-----------------------------------------------------------|
| Impressions            | `analytic_event` of type `impression`                     |
| Opens / Open rate      | Email opens via SendGrid webhook                          |
| Clicks / CTR           | SendGrid + Plausible UTM matches                          |
| Conversions / CPL      | Custom KPI target (e.g. demo bookings)                    |
| Spend                  | Reported by ad platforms (today: from `metric_value` on `analytic_event`) |
| Unsubscribes / rate    | SendGrid webhook                                          |
| Bounces / Spam complaints | SendGrid webhook                                       |

Each source shows a **freshness** badge — *e.g.* "Email — synced
2 min ago" — alongside the documented latency for that provider.

### Anomalies (E10-S02)

The Analytics agent watches every hot metric on a rolling 14-day
median. If today's value deviates by more than 3σ, an anomaly fires:

- **Severity `critical`** — `unsubscribe`, `bounce`,
  `spam_complaint`. Owner + manager get a notification (via audit_log
  marker today; email/Slack transport is on the roadmap).
- **Severity `warning`** — engagement metrics (open, click, reply).

**Dismissing** silences the same metric for 24h. Only admins can
dismiss.

**Auto-pause** — if your admin has enabled
`auto_pause_on_critical_anomaly` and two consecutive critical
anomalies hit the same metric, MAS flips the campaign to `paused`
and writes an audit row.

### Recommendations (E10-S03 / E10-S05)

After ≥ 5 days of data, the agent may surface a `budget_shift`
proposal. You'll see it under **Recommendations** on the campaign
detail page.

Each recommendation includes:
- **From / to channel** with current + proposed allocations
- **Confidence** — `low` / `medium` / `high`
- **Rationale** — the math behind it ("LinkedIn cost-per-conversion
  is 4× Email's over the last 5 days")
- **Predicted uplift** — projected % increase in conversions

**Accept** flips the campaign_channel_budget rows; the next dispatch
wave reads the new allocation. **Reject** keeps things as-is.

The default view hides recommendations with predicted uplift below
5%; toggle `Show all` to see them.

---

## 13. End-of-campaign report + CSV export

When the campaign completes (manual `complete_campaign` transition or
automatic at end_date), MAS auto-generates an end-of-campaign report.
Find it at `/ui/campaigns/{id}/report`.

### Sections
1. **Objectives** — what the brief said
2. **KPIs vs target** — observed vs target, with delta %
3. **Channel breakdown** — impressions / opens / clicks / conversions / spend per channel
4. **A/B tests** — winner + confidence + lift for each test
5. **Anomalies** — non-dismissed first, then dismissed
6. **Recommendations** — applied + rejected lists
7. **Custom KPIs** — your defined metrics with their values
8. **Spend reconciliation** — committed vs invoiced if reconciliation has run
9. **Spend total**

### Versioning
- Each regenerate creates a new version. The latest one is the default
  view. Prior versions are addressable via
  `/api/campaigns/{id}/reports/{report_id}`.
- "No data" is preserved as `null` (not `0`) — important when sharing
  with stakeholders.

### CSV export
Click **Export CSV** on the report page, or hit
`/api/campaigns/{id}/reports/latest.csv` directly. Output is a
section/key/value flattening — useful for pasting into Sheets / Excel.

PDF export is on the roadmap.

---

## 14. Custom KPIs

If MAS's built-in metrics don't cover your story, define a custom KPI:

`/api/custom-kpis` (or the UI when available) accepts a small formula:

```json
{
  "name": "demo_clicks_7d",
  "formula": {
    "event_type": "click",
    "filters": [
      {"path": "payload.utm_content", "op": "eq", "value": "demo"}
    ],
    "window_days": 7
  }
}
```

Supported filter operators: `eq`, `neq`, `in`, `not_in`, `contains`.
Filter `path` can be `payload.<key>` (JSONB nested lookup) or
`channel_id` / `event_type`.

**Data absence is preserved.** A KPI that references a missing event
type returns `value: null, missing_event: true` rather than `0`. Your
report will show "no data" instead of falsely claiming zero.

**Soft delete** — deleting a custom KPI doesn't remove historical
report numbers; the KPI just stops appearing in new reports.

---

## 15. Pause / resume / complete

### Pause
Manager or admin. Cancels queued dispatch tasks; running ones drain.
Status flips to `paused`. Useful when:
- An anomaly fires and you want to investigate
- A provider integration broke
- You spotted a problem in published copy

### Resume
Re-enqueues dispatch tasks for assets whose touchpoint slots are still
in the future. Assets whose slots elapsed during the pause are marked
`failed` with `skip_reason=slot_elapsed_during_pause`.

### Complete
Marks the campaign `completed`. Triggers automatic report generation.
After completion + a `matched` reconciliation, spend on this campaign
becomes read-only.

---

## 16. FAQ + troubleshooting

**"Why didn't an asset publish?"**
Check its status. Common reasons:
- `failed` with `skip_reason` — compliance blocker, slot elapsed,
  provider rejection, or all recipients suppressed
- `pending_approval` — waiting on you
- Frequency cap hit — see the dispatch_attempt rows for `skipped` status

**"Why is my KPI showing 'no data'?"**
The event type hasn't been recorded yet for this campaign. Different
from `0` — which means events have been recorded but none matched
your filter.

**"Recipients are still seeing the losing variant after I promoted."**
A/B assignments are permanent per recipient. After promotion, *new*
recipients hashed in will all go to the winner; existing ones keep
their assignment. This is by design — switching mid-flight contaminates
measurement.

**"The Strategist's proposal is way off."**
Tighten the brief. The single highest-leverage edit is the `objective`
+ `brief` text. The Strategist also respects any `tenant_constraint`
rows your admin has set (forbidden channels, hard caps).

**"Where's the audit trail for this approval?"**
Every approve / reject / regenerate / promotion / accept-recommendation
writes a row to `audit_log`. Ask an admin to query for your asset's id.

**"Can I delete a campaign?"**
Soft delete only — you can mark a campaign `completed` early. Hard
delete requires admin + DB access. By design: audit trails are
append-only.

**"How do I bulk-upload contacts?"**
Use the CSV upload — there's no row limit at the application layer.
For very large lists (~100k+) batch them and upload in chunks.

---

## Need more?

- **Administration Guide** — installation, integrations, OIDC, security
- **Technical Deep Dive** ([deck](../marketing/deck_technical.md)) — architecture, data model, security review
- **API docs** — once signed in, visit `/docs` for the live OpenAPI spec

Found a problem? Hit your admin first; if it's a bug, raise it via
your support channel with the campaign id and approximate timestamp.
