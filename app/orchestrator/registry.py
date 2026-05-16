"""Skill -> handler registry. Workers dispatch tasks via `get_handler`."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task

# A handler receives the active DB session and the claimed task. It returns
# the JSON-serialisable output_data payload (or None) on success, or raises
# on failure (which the worker turns into fail()).
HandlerFn = Callable[[AsyncSession, Task], Awaitable[dict[str, Any] | None]]

_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(skill_name: str, handler: HandlerFn) -> None:
    """Register `handler` under `skill_name`. Last write wins."""
    _HANDLERS[skill_name] = handler


def get_handler(skill_name: str) -> HandlerFn | None:
    return _HANDLERS.get(skill_name)


def registered_skills() -> list[str]:
    return sorted(_HANDLERS.keys())
