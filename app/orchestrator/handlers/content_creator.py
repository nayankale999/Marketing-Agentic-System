"""Queue handler for the Content Creator agent (skill: content_creator.generate_asset)."""

from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.content_creator import generate_asset
from app.db.models import Task
from app.settings.config import get_settings
from app.tools.copywriting import CopywritingTool
from app.tools.seo import SeoAnalysisTool


class ContentCreatorNotConfiguredError(Exception):
    """Raised when ANTHROPIC_API_KEY is unset — caller surfaces a 503."""


def _build_copywriting_tool() -> CopywritingTool:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ContentCreatorNotConfiguredError(
            "ANTHROPIC_API_KEY is not configured; content creator is unavailable"
        )
    return CopywritingTool(
        client=AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.copywriting_model,
    )


async def content_creator_generate_asset_handler(
    session: AsyncSession, task: Task
) -> dict[str, Any]:
    """Skill: `content_creator.generate_asset`.

    Expects task.input_data = {asset_id, campaign_id}. Returns the per-asset
    summary into `task.output_data` (asset_id, status, brand_check_pass,
    length_warning, seo_score)."""
    inputs = task.input_data
    copywriting_tool = _build_copywriting_tool()
    seo_tool = SeoAnalysisTool()
    return await generate_asset(
        session,
        asset_id=UUID(inputs["asset_id"]),
        copywriting_tool=copywriting_tool,
        seo_tool=seo_tool,
    )
