"""Queue handler for the Channel Distribution agent (W28, E08-S02)."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.distribution import dispatch_email_asset
from app.db.models import Task


async def distribution_dispatch_email_handler(
    session: AsyncSession, task: Task
) -> dict[str, Any]:
    """Skill: `distribution.dispatch_email`.

    Expects task.input_data = {asset_id, campaign_id}. Returns the dispatch
    summary into task.output_data. Provider transport failures propagate so
    the queue's retry/backoff path keeps the asset in `scheduled`."""
    inputs = task.input_data
    return await dispatch_email_asset(
        session,
        asset_id=UUID(inputs["asset_id"]),
    )
