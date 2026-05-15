# MAS — Delivery Backlog

This folder is the bridge between the system diagrams (`MAS.png`, `DBSchema.png`) and engineering execution. It breaks the architecture into epics and stories with acceptance criteria, lays out the database schema, and captures the agent architecture as Mermaid.

The target stack is **Python 3.12 + FastAPI + Claude Agent SDK + PostgreSQL 15+**. Scope is **MVP + Phase 2 (outline) + NFRs**.

## Intended audience

| Role | Start here |
|------|-----------|
| Engineering lead / Product manager | [epics.md](epics.md) — scope, sequencing, coverage matrix |
| Developer picking up a story | [stories/](stories/) — epic file for the area you own |
| Database reviewer / DBA | [schema.sql](schema.sql) |
| Architect / Staff engineer | [architecture.md](architecture.md) — agent map + lifecycle state machines |

## How to use this backlog

1. **Scope a release**: pick epics from `epics.md`, filter by priority (`Must` / `Should` / `Could`) and phase (`MVP` / `Phase 2`).
2. **Estimate**: open the epic's story file, size each story, adjust ACs with your team.
3. **Build**: each story carries independently-verifiable Given/When/Then criteria. A story is "done" when every AC passes.
4. **Trace back to the diagrams**: every epic references the part of `MAS.png` or `DBSchema.png` it implements. When requirements shift, update the diagrams first, then propagate to the affected epic/story.

## Conventions

- **Story IDs**: `E{epic}-S{story}` (e.g. `E06-S03`). Stable forever — do not renumber.
- **Priority**: MoSCoW (`Must`, `Should`, `Could`) scoped to MVP unless tagged Phase 2.
- **AC format**: Given / When / Then, 3–6 per story, each testable without further clarification.
- **Dependencies**: declared inline on each story; the epic register aggregates the cross-epic graph.

## Scope

- **MVP (in depth)** — epics E01 through E16, covering ingestion, orchestration, the five specialist agents, distribution, A/B testing, analytics, UI, auth, audit, and NFRs.
- **Phase 2 (outline)** — `P2-optimisation.md` and `P2-personalisation.md` carry shape-only stories to inform sequencing. These are not yet ready to build.
- **Phase 3** — deliberately excluded from this backlog. Revisit after MVP is in production.

## Files

| File | Purpose |
|------|---------|
| `README.md` | This file |
| `epics.md` | Epic register, dependency graph, diagram coverage matrix |
| `stories/E01-data-ingestion.md` … `E16-nfr-security-compliance.md` | MVP stories with acceptance criteria |
| `stories/P2-optimisation.md`, `stories/P2-personalisation.md` | Phase 2 outline stories |
| `schema.sql` | PostgreSQL DDL — runnable against a fresh database |
| `architecture.md` | System context, agent map, campaign + content lifecycle state machines (Mermaid) |

## Keeping this in sync

When the architecture changes:

1. Update `MAS.png` / `DBSchema.png` (and their Excalidraw source).
2. Update affected epic entries in `epics.md` (scope notes, story counts).
3. Update story files under `stories/`.
4. If the change touches data, update `schema.sql` and note the migration in the affected story.
5. If the change touches flow, update `architecture.md`.

Treat drift between diagrams and backlog as a bug. The diagrams are the source of intent; this folder is the source of execution.
