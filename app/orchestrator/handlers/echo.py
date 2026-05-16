"""No-op echo handler: returns the task's input_data verbatim.

Used by the W6 acceptance tests to prove the queue + worker loop end-to-end.
Will also be used by W7 (state machine) and W8 (tool registry) as a stand-in
for real specialist agents that don't exist yet.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task


async def echo_handler(session: AsyncSession, task: Task) -> dict[str, Any]:
    """Return the task's input_data as the output."""
    del session  # unused; handlers may use the session for DB work
    return dict(task.input_data)
