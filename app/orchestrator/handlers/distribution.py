"""Queue handlers for the Channel Distribution agent (W28 + W30)."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.distribution import dispatch_email_asset, dispatch_social_asset
from app.db.models import Task


async def distribution_dispatch_email_handler(
    session: AsyncSession, task: Task
) -> dict[str, Any]:
    """Skill: `distribution.dispatch_email` (W28).

    Expects task.input_data = {asset_id, campaign_id}. Returns the dispatch
    summary into task.output_data. Provider transport failures propagate so
    the queue's retry/backoff path keeps the asset in `scheduled`."""
    inputs = task.input_data
    return await dispatch_email_asset(
        session,
        asset_id=UUID(inputs["asset_id"]),
    )


async def distribution_dispatch_social_handler(
    session: AsyncSession, task: Task
) -> dict[str, Any]:
    """Skill: `distribution.dispatch_social` (W30, E12-S03).

    Expects task.input_data = {asset_id, campaign_id}. Returns
    {provider_post_id, url} into task.output_data. OAuthRevokedError
    propagates after the agent has paused the campaign; the queue then
    records the failure in agent_log."""
    inputs = task.input_data
    return await dispatch_social_asset(
        session,
        asset_id=UUID(inputs["asset_id"]),
    )
