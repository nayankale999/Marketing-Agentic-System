"""SQLAlchemy event listeners that auto-write audit_log rows on insert.

The listeners receive a sync `Connection` from the ORM unit-of-work flush;
the connection is the same one running the parent flush, so the audit row
lands in the same transaction as the change it audits. Actor identity is
read from the contextvars in `app.audit.context`.

Call `register()` once at process startup (e.g. from `app.api.app` and
`tests/conftest.py`). The function is idempotent.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import event, insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper

from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot
from app.db.models import Agent, AppUser, AuditLog, Campaign, Tenant

_REGISTERED = False


def _make_insert_listener(
    entity_kind: str, tenant_id_getter: Callable[[Any], UUID]
) -> Callable[[Mapper[Any], Connection, Any], None]:
    def _on_insert(mapper: Mapper[Any], connection: Connection, target: Any) -> None:
        connection.execute(
            insert(AuditLog).values(
                tenant_id=tenant_id_getter(target),
                actor_kind=current_actor_kind.get(),
                actor_id=current_actor_id.get(),
                entity_kind=entity_kind,
                entity_id=target.id,
                action="created",
                after_state=column_snapshot(target),
                # The column is named `metadata` in the DB but mapped as
                # `extra_metadata` on the ORM class (because `metadata` is
                # reserved by DeclarativeBase). The insert kwargs use the
                # ORM-attribute name.
                extra_metadata={},
            )
        )

    return _on_insert


def register() -> None:
    """Register `after_insert` listeners on every tracked model. Idempotent."""
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    bindings: list[tuple[type, str, Callable[[Any], UUID]]] = [
        (Tenant, "tenant", lambda t: t.id),
        (AppUser, "app_user", lambda t: t.tenant_id),
        (Agent, "agent", lambda t: t.tenant_id),
        (Campaign, "campaign", lambda t: t.tenant_id),
    ]
    for model, kind, getter in bindings:
        event.listen(model, "after_insert", _make_insert_listener(kind, getter))
