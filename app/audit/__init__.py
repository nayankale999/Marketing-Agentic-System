"""Append-only audit log: writer, actor context, and ORM event listeners.

Call `register_listeners()` once at process startup (FastAPI factory and
tests/conftest do this). Once registered, every INSERT on a tracked domain
table auto-writes an audit_log row in the same transaction.
"""

from app.audit.context import (
    ActorKind,
    actor_context,
    current_actor_id,
    current_actor_kind,
)
from app.audit.listeners import register as register_listeners
from app.audit.writer import column_snapshot, write_audit

__all__ = [
    "ActorKind",
    "actor_context",
    "column_snapshot",
    "current_actor_id",
    "current_actor_kind",
    "register_listeners",
    "write_audit",
]
