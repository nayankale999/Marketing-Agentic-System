"""Queue handler for the audience-targeting agent."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.audience_targeting import materialise
from app.db.models import Task


async def audience_materialise_handler(session: AsyncSession, task: Task) -> dict[str, Any]:
    """Skill: `audience_targeting.materialise`.

    Expects task.input_data = {campaign_id, audience_name, criteria}.
    Stamps the resulting audience_id + member_count into task.output_data.
    """
    inputs = task.input_data
    return await materialise(
        session,
        campaign_id=UUID(inputs["campaign_id"]),
        audience_name=inputs["audience_name"],
        criteria=inputs["criteria"],
    )
