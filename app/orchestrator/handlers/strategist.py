"""Queue handler for the Campaign Strategist agent (skill: campaign_strategist.propose)."""

from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._strategist_planner import StrategistPlanner
from app.agents.strategist import propose
from app.db.models import Task
from app.settings.config import get_settings


class StrategistNotConfiguredError(Exception):
    """Raised when ANTHROPIC_API_KEY is unset — caller surfaces a 503."""


def _build_planner() -> StrategistPlanner:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise StrategistNotConfiguredError(
            "ANTHROPIC_API_KEY is not configured; campaign strategist is unavailable"
        )
    return StrategistPlanner(
        client=AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.strategist_model,
    )


async def campaign_strategist_propose_handler(
    session: AsyncSession, task: Task
) -> dict[str, Any]:
    """Skill: `campaign_strategist.propose`.

    Expects task.input_data = {campaign_id, triggered_by_user_id?}.
    Returns the persisted proposal summary into `task.output_data`.
    """
    inputs = task.input_data
    triggered_by = inputs.get("triggered_by_user_id")
    planner = _build_planner()
    return await propose(
        session,
        campaign_id=UUID(inputs["campaign_id"]),
        planner=planner,
        triggered_by_user_id=UUID(triggered_by) if triggered_by else None,
    )
