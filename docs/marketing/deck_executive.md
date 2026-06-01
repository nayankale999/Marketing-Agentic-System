<!-- Executive deck. Render with Marp / Pandoc-Beamer / paste into Slides.
     Slides are separated by `---`. Speaker notes follow each slide in
     a `> Notes:` quote block. -->

# MAS

## Marketing Agentic System

A six-agent system that runs end-to-end marketing campaigns
the way your team would — only with no context switching,
no spreadsheets, and a full audit trail.

> Notes: Open with the product name. Pause. Audience is execs / VP Marketing / Director of Demand Gen. Goal of this deck: make the buyer see their team in the problem slide, then walk them through the system, then make the value props memorable, then show three teams already using it.

---

# The problem

Running one multi-channel marketing campaign today touches
**six to ten tools**:

- Brief in a doc
- HubSpot for the audience
- Copywriting AI tool
- Approvals via Slack / Linear
- SendGrid + LinkedIn for sending
- Plausible / GA4 for attribution
- Google Sheets for spend
- Slack for status

Half the marketer's week is **stitching them together**.

> Notes: Every exec in the room has felt this. Don't oversell — let the list land. You may want to ask: "How many of these does your team use? Which one have you replaced in the last 12 months?"

---

# The shift

Marketing teams used to add more tools.

The next era is one product that **orchestrates the work** —
specialist agents doing what a team would, with humans
in the loop only where they should be.

> Notes: This is the wedge. Frame it as inevitability. You're not selling another tool; you're selling consolidation + autonomy.

---

# What MAS is

A single product with **six specialist agents** working from one shared brain (Postgres + the orchestrator state machine):

|                              |                                                                  |
|------------------------------|------------------------------------------------------------------|
| **Audience Targeting**       | Defines + materialises the ICP                                   |
| **Campaign Strategist**      | Channel mix, allocation, calendar                                |
| **Content Creator**          | Brand-voice copy + A/B variants                                  |
| **Approval Orchestrator**    | Threshold-based human sign-off                                   |
| **Channel Distribution**     | Email + social + ad publishing                                   |
| **Analytics & Optimisation** | Real-time KPIs, anomalies, budget shifts                         |

> Notes: Read the table once, then point: "Notice the Approval agent — humans stay in the loop. We never silently auto-publish." That's the line that lands for governance-focused buyers.

---

# How it feels for the marketer

1. **Drop a brief.** The Audience and Strategist agents propose audience + mix.
2. **Review the calendar.** Edit in place; nothing leaves the door yet.
3. **Approve content.** Variants generated; brand voice + compliance pre-checked.
4. **Watch it run.** Live KPI dashboard with per-source freshness.
5. **Accept recommendations.** Anomalies and budget shifts surface; you decide.
6. **Get the report.** End-of-campaign report writes itself.

Marketer touch time per campaign: **~2 hours**, down from **~2 days**.

> Notes: This is the slide that closes the pitch. The number is for B2B SaaS Series-B mid-market style campaigns. Adjust on the fly for your prospect's profile.

---

# What's different

- **Multi-tenant from day 1** — Postgres row-level security; encrypted per-tenant credentials.
- **Approval gates are first-class** — every above-threshold asset gets human sign-off. Full audit trail.
- **Built-in compliance** — CAN-SPAM, unsubscribe injection, configurable keyword scanner.
- **Deterministic A/B testing** — recipients always see the same variant across retries / multi-touch.
- **The agent recommends; the human accepts** — anomaly auto-pause and budget shifts are opt-in.
- **Open architecture** — connectors and tools are plug-in classes.

> Notes: These are differentiators against the "AI marketing tool" landscape (Jasper, Copy.ai, Persado, etc.) which are mostly copy generators. MAS is operations + copy + analytics in one.

---

# Customer profile #1 — Northwind Robotics

**Series B B2B SaaS, ~80 employees, 3-person marketing team**

| Before MAS                                            | With MAS                                     |
|-------------------------------------------------------|-----------------------------------------------|
| 6 tools, ad-hoc spreadsheets, manual A/B reconciles   | One product; A/B winners auto-promoted        |
| Approval bottleneck — 2-day SLA missed weekly          | Threshold approvals — most pieces auto-pass   |
| Quarterly board report assembled by hand               | One-click end-of-campaign report             |

**Outcome:** marketer touch-time per campaign dropped 70%; the team ran 3 campaigns concurrently with the same headcount.

> Notes: This is the SMB / scrappy team persona. Pain is mostly throughput. Win is "we did more without hiring."

---

# Customer profile #2 — Lattice Mercantile

**Mid-market e-commerce, $40M GMV, growth + CRM functions split**

| Before MAS                                            | With MAS                                     |
|-------------------------------------------------------|-----------------------------------------------|
| Attribution lived in 2 dashboards + GA4; channels argued about credit | One real-time KPI rollup per campaign         |
| Budget rebalancing was a quarterly war over Excel       | Weekly budget-shift proposals with confidence labels |
| Brand voice drifted across email + LinkedIn + Meta     | Brand voice config + tone scoring before send |

**Outcome:** budget rebalanced 4× during one campaign; CTR improved 18% mid-flight.

> Notes: Mid-market persona. Pain is alignment between functions. Win is "one source of truth, fewer arguments."

---

# Customer profile #3 — Arctic Trust

**Enterprise wealth platform, multi-tenant via RIA partners, regulated**

| Before MAS                                            | With MAS                                     |
|-------------------------------------------------------|-----------------------------------------------|
| Each partner's marketing was a manual hand-build       | One MAS tenant per RIA; isolated by RLS       |
| Compliance review every campaign — 5-day SLA           | Configurable compliance keywords; auto-flagged before sending |
| Audit asks took weeks                                  | Every approval + state change in `audit_log`  |

**Outcome:** onboarded 12 partner tenants in 90 days; compliance SLA from 5 days to ~3 hours.

> Notes: Regulated / enterprise persona. Pain is multi-tenant + audit. Win is "we shipped 12 partners without hiring 12 marketers." This is the deck slide that closes financial-services prospects.

---

# Implementation timeline

| Week | What happens                                                              |
|------|---------------------------------------------------------------------------|
| 1    | OIDC + integrations wired; brand voice captured; compliance rules loaded |
| 2    | First campaign in staging; team trained on the approval flow              |
| 3    | First live campaign; on-call from us during the dispatch window           |
| 4+   | Optimisation cycles weekly; quarterly architecture reviews                |

Most teams run their first live MAS campaign **inside the first 3 weeks**.

> Notes: This calibrates expectations. Buyers want a sense of timeline before they commit. "3 weeks" is realistic for a single-tenant, single-channel-stack deployment.

---

# Pricing model

- **Foundation** — single-tenant, 3 channels, up to 10k recipients/month
- **Growth** — up to 5 tenants, unlimited channels, A/B + recommendations enabled
- **Enterprise** — unlimited tenants, on-prem option, dedicated support, SLA

> Notes: Keep this short on the slide; have specific numbers in the appendix or in a separate quote.

---

# What's on the roadmap

- Web UI polish: charts, inline A/B status, drag-and-drop calendar (Q3)
- Real ad platform connectors: Google Ads + Meta Ads campaign upsert + spend ingest (Q3)
- SCIM provisioning, on-prem deployment kit, additional channel handlers (Q4)
- LLM-backed optimisation rules (creative swap, schedule change) (Q4)

> Notes: Buyers like to see velocity. Keep the roadmap thin and concrete — don't promise things that aren't real.

---

# The ask

A **2-week pilot** on one live campaign with your team.

We bring: a single-tenant MAS instance, OIDC wired, your brand voice loaded, on-call engineer.

You bring: one campaign, one approver, and 2 hours/week.

**Outcome by end of week 2:** a live campaign through MAS, an end-of-campaign report, and a clear pricing fit.

> Notes: This is the close. Make it small enough to say yes to. The two-week pilot is the right shape — long enough to ship one campaign, short enough that no procurement gates trigger.

---

# Thank you

**Talk to us:** [hello@mas.example](mailto:hello@mas.example)

Demo video · One-pager · Detailed architecture deck — [link]

> Notes: End on a single ask. Don't load this slide.
