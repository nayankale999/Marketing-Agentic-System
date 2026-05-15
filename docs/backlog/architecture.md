# MAS — Agent Architecture

Four diagrams. Read them in order.

1. **System context** — where MAS sits between users, external systems, and its own internals.
2. **Agent map** — the orchestrator plus the five specialist agents from `MAS.png`, with the shared skills/tools layer and the approval gate explicit.
3. **Campaign lifecycle state machine** — how a campaign moves through its states.
4. **Content asset lifecycle state machine** — how a content asset moves from brief to publish.

All diagrams are Mermaid. They render natively in GitHub / GitLab / most IDEs and any Markdown tool with Mermaid support.

---

## 1. System context

MAS is a Python backend with a FastAPI surface, a Claude-Agent-SDK-based agent layer, and integrations into CRM, social platforms, ad platforms, and email providers. Users are marketers, marketing managers, and admins. The orchestrator is the traffic controller — nothing calls a specialist agent directly from a browser click; every action flows through the orchestrator's task queue.

```mermaid
flowchart LR
    subgraph Users
        Marketer([Marketer])
        Manager([Marketing Manager])
        Admin([RevOps / Compliance Admin])
    end

    subgraph MAS["Marketing Agentic System"]
        direction TB
        FE[Web Frontend<br/>Next.js or Streamlit]
        API[FastAPI Layer]
        ORCH[Orchestrator<br/>state machine + task queue]
        AI[Agent Layer<br/>Claude Agent SDK + tool registry]
        DB[(Postgres<br/>D1: Campaign DB)]
        OBJ[(Object Storage<br/>D2: Content Store)]
        WH[(Analytics Warehouse<br/>D3)]
        MSG[[Notification Service]]
    end

    subgraph External["External Systems"]
        CRM[CRM System]
        Social[Social Platforms<br/>LinkedIn / X / Meta]
        Ads[Ad Platforms<br/>Google Ads / Meta Ads]
        Email[Email Provider<br/>SendGrid / SES / Postmark]
        Web[Web Analytics<br/>GA4 / Plausible]
    end

    Marketer --> FE
    Manager --> FE
    Admin --> FE
    FE --> API
    API --> ORCH
    ORCH --> AI
    ORCH --> DB
    AI --> DB
    AI --> OBJ
    API --> DB
    ORCH --> MSG
    ORCH <--> CRM
    ORCH <--> Social
    ORCH <--> Ads
    ORCH <--> Email
    ORCH <--> Web
    Email --> WH
    Social --> WH
    Ads --> WH
    Web --> WH
    WH --> AI
    MSG --> Marketer
    MSG --> Manager
```

---

## 2. Agent map

One orchestrator, five specialist agents, six shared tools, one approval gate. The orchestrator holds the state machine; every arrow into an agent is a scheduled task, every arrow out is an event. The approval gate (`Approval Orchestrator`) is a hard barrier — outbound publishes (content, dispatch) cannot bypass it in MVP.

```mermaid
flowchart TB
    ORCH(((Marketing Orchestrator)))

    subgraph Plan["Plan"]
        A1[1 · Campaign Strategist]
        A2[2 · Audience Targeting]
    end

    subgraph Create["Create"]
        A3[3 · Content Creator]
        A4{{4 · Approval Orchestrator}}
    end

    subgraph Distribute["Distribute"]
        A5[5 · Channel Distribution]
    end

    subgraph Learn["Learn & Optimise"]
        A6[6 · Analytics & Optimisation]
    end

    subgraph Tools["Shared Skills / Tools"]
        T1[SEO Analysis]
        T2[Copywriting]
        T3[A/B Testing]
        T4[Segmentation]
        T5[Social Media API]
        T6[Email Automation]
    end

    ORCH --> A1
    ORCH --> A2
    A1 --> A3
    A2 --> A1
    A2 --> A3
    A3 --> A4
    A4 -->|approved| A5
    A4 -->|rejected| A3
    A5 --> A6
    A6 -.->|optimisation suggestions| A1
    A6 -.->|variant suggestions| A3
    A6 -.->|budget rebalance| A5

    A1 -. uses .-> T4
    A2 -. uses .-> T4
    A3 -. uses .-> T1
    A3 -. uses .-> T2
    A3 -. uses .-> T3
    A5 -. uses .-> T5
    A5 -. uses .-> T6
    A6 -. uses .-> T3

    classDef gate fill:#fff3cd,stroke:#856404,stroke-width:2px
    class A4 gate
```

**Reading this diagram:**
- Solid arrows are first-class orchestrated transitions.
- Dashed arrows are advisory — they influence later runs but don't directly drive execution.
- Dotted `uses` arrows are tool invocations available to that agent through the SDK tool registry.
- The Approval Orchestrator is a hard gate for any outbound action in MVP. Phase 2 may introduce auto-approval thresholds for known-safe variant tweaks (see `P2-optimisation.md`).

---

## 3. Campaign lifecycle state machine

From brief to closure. Transitions with `[guard]` conditions only fire when the guard passes — otherwise the campaign stays put.

```mermaid
stateDiagram-v2
    [*] --> drafted: brief created (manual or imported)
    drafted --> audience_built: audience targeting completes
    audience_built --> strategy_set: strategist proposes channel mix + budget
    strategy_set --> content_in_production: assets queued for generation
    content_in_production --> approval_pending: drafts complete
    approval_pending --> ready_to_launch: all required assets approved
    approval_pending --> content_in_production: any required asset rejected
    ready_to_launch --> live: scheduled time reached / manual launch
    live --> optimising: analytics agent proposes adjustment
    optimising --> live: adjustment applied
    live --> paused: manual pause OR budget cap hit OR compliance flag
    paused --> live: resume
    live --> completed: end_date reached OR budget exhausted
    optimising --> completed: end_date reached
    paused --> completed: manual stop OR end_date reached
    completed --> [*]
```

**Key transitions to note:**
- `approval_pending -> content_in_production` on rejection: a single rejected required asset blocks launch and bounces the campaign back to the Content Creator. Optional assets do not block.
- `live <-> optimising`: optimisation is mid-flight, not a separate phase. The campaign re-enters `live` once the change is applied; budget/creative deltas are journaled.
- `live -> paused` on `compliance flag`: triggered by the Audit cross-cutting check (see E15) when an unsubscribe rate or content-policy threshold is crossed.

---

## 4. Content asset lifecycle state machine

Content assets live within a campaign but progress independently. Each asset (email, social post, ad creative, blog) has its own lifecycle; the campaign waits on the slowest required asset.

```mermaid
stateDiagram-v2
    [*] --> requested: brief generated for asset
    requested --> generating: content creator picks up task
    generating --> drafted: model output complete + SEO/copy checks pass
    generating --> requested: tool-level failure, retry
    drafted --> pending_approval: queued to approval orchestrator
    pending_approval --> approved: reviewer approves (with or without edits)
    pending_approval --> rejected: reviewer rejects with reason
    rejected --> generating: regenerate with rejection feedback
    approved --> scheduled: distribution agent picks slot
    scheduled --> published: dispatch succeeds
    scheduled --> failed: dispatch error, retry budget exhausted
    published --> measuring: analytics events flowing
    measuring --> variant_winner: A/B significance reached
    measuring --> archived: campaign completed
    variant_winner --> archived: campaign completed
    failed --> [*]
    archived --> [*]
```

**Key transitions to note:**
- `rejected -> generating`: the rejection reason is fed back to the Content Creator agent as additional context for the regenerate prompt; assets do not silently re-enter the queue.
- `scheduled -> failed`: a dispatch failure only ends the asset's lifecycle after the retry budget is exhausted. Each retry is journaled in `agent_logs`.
- `measuring -> variant_winner`: applies only to assets in an active A/B test (see E09); non-variant assets go straight from `measuring` to `archived` on campaign completion.

---

## Cross-cutting concerns

These are not in the agent map because they wrap every agent, not slot between them.

- **Audit (E15)** — every state change, every model call, every external API call writes to `agent_logs` (per-task) or `analytic_events` (per-outcome). Append-only; no UPDATE/DELETE rights for the app role.
- **RBAC (E14)** — every API call carries `tenant_id` and `user_id`; every domain table is scoped by `tenant_id`. The orchestrator refuses to schedule a task that crosses tenant boundaries.
- **Compliance (E16)** — unsubscribe links, suppression lists, GDPR data-subject requests, CAN-SPAM identity blocks, retention policies. The Channel Distribution agent consults the suppression list before every send.
- **Observability (E16)** — OpenTelemetry traces span the orchestrator, agent calls, tool calls, and external API calls. The campaign-id and task-id are propagated as span attributes.
