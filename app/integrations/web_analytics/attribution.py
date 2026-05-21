"""UTM → campaign_id resolver.

W16 baseline: case-insensitive match against `campaign.name`. The real
production version will read a dedicated `campaign.utm_campaign` slug
(added in a follow-up migration); for now the slug is just the name.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign


async def resolve_campaign_by_utm(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    utm_campaign: str | None,
) -> UUID | None:
    """Return the campaign_id whose name matches `utm_campaign` (or None)."""
    if not utm_campaign:
        return None
    stmt = (
        select(Campaign.id)
        .where(Campaign.tenant_id == tenant_id, Campaign.name.ilike(utm_campaign))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
