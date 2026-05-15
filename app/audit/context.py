"""Per-request actor context for audit writes.

A FastAPI dependency (typically `get_current_user`) sets these contextvars
near the start of a request. Anything that mutates state later in the same
async task — including SQLAlchemy event listeners that fire on flush — reads
them to attribute the change.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Literal
from uuid import UUID

ActorKind = Literal["user", "agent", "system"]

current_actor_kind: ContextVar[ActorKind] = ContextVar("current_actor_kind", default="system")
current_actor_id: ContextVar[UUID | None] = ContextVar("current_actor_id", default=None)


@contextmanager
def actor_context(kind: ActorKind, actor_id: UUID | None) -> Iterator[None]:
    """Temporarily set the actor for a code block (CLI scripts, tests, agents)."""
    kind_token: Token[ActorKind] = current_actor_kind.set(kind)
    id_token: Token[UUID | None] = current_actor_id.set(actor_id)
    try:
        yield
    finally:
        current_actor_kind.reset(kind_token)
        current_actor_id.reset(id_token)
