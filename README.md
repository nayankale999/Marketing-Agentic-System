# Marketing Agentic System

An agentic system that plans, creates, distributes, and optimises marketing campaigns. One orchestrator + five specialist agents on top of FastAPI, Postgres, and the Claude Agent SDK.

## Where to read

- **Architecture, schema, backlog:** [docs/backlog/](docs/backlog/) — start with [README](docs/backlog/README.md) → [architecture.md](docs/backlog/architecture.md) → [epics.md](docs/backlog/epics.md).
- **Build plan:** [docs/build-plan.md](docs/build-plan.md) — five delivery slices, Slice 1 fully decomposed.
- **System diagrams:** [MAS.png](MAS.png), [DBSchema.png](DBSchema.png).

## Quickstart

Prerequisites: Python 3.12, [uv](https://github.com/astral-sh/uv), Docker.

```bash
make install   # uv sync --all-extras
make dev       # docker compose up postgres+otel+mailhog, then uvicorn on :8000
make test      # pytest
```

`curl localhost:8000/health` should return `{"status":"ok"}`.

See `make help` for the full target list.
