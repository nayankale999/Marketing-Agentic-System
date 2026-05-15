"""Helpers for capturing model state and writing audit_log entries.

The bulk of audit writes are driven by SQLAlchemy event listeners in
`app.audit.listeners`. This module exposes `write_audit()` for explicit
calls (e.g. login events, soft deletes) and `column_snapshot()` used by
the listeners to build before/after JSON.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.context import ActorKind
from app.db.models import AuditLog


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(v) for v in value]
    return str(value)


def column_snapshot(target: Any) -> dict[str, Any]:
    """Capture an ORM instance's column values as a JSON-safe dict.

    Relationship attributes are ignored — only mapped columns are included.
    """
    columns = inspect(target).mapper.columns.keys()
    return {col: _to_jsonable(getattr(target, col, None)) for col in columns}


def write_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_kind: ActorKind,
    actor_id: UUID | None,
    entity_kind: str,
    entity_id: UUID,
    action: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Append a row to audit_log. Caller is responsible for the surrounding tx.

    Use this for events that don't correspond to a tracked ORM mutation, e.g.
    login, logout, manual state transitions, exports.
    """
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=action,
        before_state=before_state,
        after_state=after_state,
        extra_metadata=metadata or {},
    )
    session.add(entry)
    return entry
