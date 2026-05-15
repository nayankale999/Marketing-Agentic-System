"""Append-only agent log: per-task telemetry the orchestrator and agents write.

Unlike `audit_log` (driven by ORM event listeners on domain mutations),
`agent_log` is written explicitly by agent code: every model call, tool call,
retry decision, and final outcome lands here.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentLog


async def agent_log_emit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    task_id: UUID | None,
    action: str,
    log_data: dict[str, Any] | None = None,
    severity: str = "info",
) -> AgentLog:
    """Append a row to agent_log. Caller is responsible for the surrounding tx."""
    entry = AgentLog(
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        action=action,
        log_data=log_data or {},
        severity=severity,
    )
    session.add(entry)
    return entry
