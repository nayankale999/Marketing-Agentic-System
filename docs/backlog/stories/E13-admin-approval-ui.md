# E13 — Admin & Approval UI

**Diagram reference:** Not in `MAS.png` (system frontend)
**Priority:** Must (MVP)
**Dependencies:** E03 (campaigns), E07 (approval), E10 (reports)

The minimal UI surface that lets a marketer drive the system without curl-ing the API. MVP is intentionally narrow: campaign list/detail, audience builder, content review, reports, integrations settings.

---

### E13-S01 — Campaign dashboard

**As a** marketer,
**I want** a dashboard listing my campaigns with state and KPIs,
**So that** I know what to attend to without opening each one.

Priority: Must
Dependencies: E03, E10

Acceptance criteria:
- Given I sign in, when the dashboard loads, then I see my campaigns grouped by state with primary KPI, owner, and last activity.
- Given a campaign needs my action (pending approval, anomaly, draft rejected), when displayed, then a clear chip surfaces the action with a link to it.
- Given I have 50+ campaigns, when scrolling, then virtualisation keeps interaction snappy (no full-list rerender on filter change).
- Given I lack access to a campaign, when listing, then it does not appear.

### E13-S02 — Campaign detail view

**As a** marketer,
**I want** a single page that drills into a campaign,
**So that** I can see brief, audience, strategy, content, calendar, run history, and reports in one place.

Priority: Must
Dependencies: E03

Acceptance criteria:
- Given I open a campaign, when the page loads, then I see tabs: Brief, Audience, Strategy, Content, Schedule, Runs, Reports.
- Given the campaign is `live`, when displayed, then a header strip shows current KPIs vs target, with a freshness timestamp.
- Given a tab has unread changes (e.g., new draft, new recommendation), when shown, then it carries a badge.
- Given I edit any field, when saved, then the change reflects in `audit_log` and the corresponding tab shows the change in its activity log.

### E13-S03 — Approval review screen

**As a** marketing manager,
**I want** a focused screen for reviewing a draft,
**So that** approval is fast and not error-prone.

Priority: Must
Dependencies: E07

Acceptance criteria:
- Given I open an asset in `pending_approval`, when displayed, then I see the asset preview (channel-specific), brief, audience summary, brand-check flags, compliance flags, and SEO score (if applicable).
- Given I edit text inline, when I click "Approve with edits", then the edit is saved as a diff and the asset moves to `approved`.
- Given I click "Reject", when I submit, then a reason field appears (required) with category dropdown.
- Given the asset is mobile-viewed, when previewed, then the channel preview adapts to the smaller viewport.

### E13-S04 — Reports view

**As a** marketing manager,
**I want** the reports tab to render the end-of-campaign report inline,
**So that** I do not have to download a PDF for routine checks.

Priority: Must
Dependencies: E10-S04

Acceptance criteria:
- Given a campaign has any data, when I open Reports, then I see the report sections (objectives, KPIs vs target, channel, A/B, anomalies, spend, recommendations) with charts.
- Given the campaign is still `live`, when shown, then numbers are flagged "in-flight" with refresh time.
- Given I click "Export PDF" or "Export CSV", when triggered, then a file downloads matching what is on screen.
- Given a chart fails to render (data gap), when shown, then it shows an empty state with the gap reason, not a stack trace.

### E13-S05 — Settings: integrations, voice, constraints

**As an** admin,
**I want** one settings area for integrations, brand voice, and tenant constraints,
**So that** onboarding a tenant is a single guided flow.

Priority: Must
Dependencies: E05-S05, E06-S02, E12

Acceptance criteria:
- Given I open Settings, when I navigate the sub-tabs, then I find: Users & Roles, Integrations, Brand Voice, Constraints, Suppression List, Audit Export.
- Given I save a config change, when written, then it is captured in `audit_log` with my user id.
- Given a config change would break an in-flight campaign (e.g., remove an active channel), when attempted, then I am warned and asked to confirm.
- Given I lack admin role, when I open Settings, then I see only the panels permitted to my role (e.g., my profile).

### E13-S06 — Notifications inbox

**As a** marketer,
**I want** a notifications inbox in the UI,
**So that** anomalies and approval requests do not depend on email working.

Priority: Should
Dependencies: E07, E10-S02

Acceptance criteria:
- Given I sign in, when notifications exist, then a badge in the top nav shows the count of unread.
- Given I open the inbox, when it loads, then I see items grouped by campaign with timestamp and direct link.
- Given I click an item, when shown, then it is marked read and offers the relevant action (approve, view anomaly, view report).
- Given a notification is critical (compliance flag, auto-pause), when received, then the inbox highlights it and a parallel email is also sent.
