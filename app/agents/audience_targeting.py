"""Audience Targeting agent (W15, E04-S01..S04 baseline).

This agent turns a criteria expression into a materialised `audience` +
`audience_member` snapshot under a campaign. It's invoked via the
orchestrator queue with skill `audience_targeting.materialise`; the API
endpoint `POST /api/campaigns/{id}/audiences` enqueues a task that the
worker picks up.

What lands later:
- Refresh semantics (E04-S03): re-materialise as a new snapshot under the
  same audience_id, archiving the old member-set.
- Exclusion of live-campaign members (E04-S04): an `exclude_live_campaigns`
  flag the agent honours.
- Size guardrails (E04-S02): warn when net is < 10 or > 80% of base list.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audiences.segmentation import build, validate_criteria
from app.db.enums import AgentKind
from app.db.models import Agent, Audience, AudienceMember, Campaign


async def ensure_audience_targeting_agent(session: AsyncSession, tenant_id: UUID) -> Agent:
    """Get-or-create the per-tenant Audience Targeting agent row."""
    existing = (
        await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == AgentKind.audience_targeting,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    agent = Agent(
        tenant_id=tenant_id,
        name="Audience Targeting",
        agent_type=AgentKind.audience_targeting,
    )
    session.add(agent)
    await session.flush()
    return agent


async def materialise(
    session: AsyncSession,
    *,
    campaign_id: UUID,
    audience_name: str,
    criteria: dict[str, Any],
) -> dict[str, Any]:
    """Run the criteria, snapshot the contacts into a new audience.

    Returns the JSON payload the queue handler stamps into `task.output_data`.
    Raises `SegmentationError` if the criteria fail validation.
    """
    validate_criteria(criteria)

    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError(f"campaign {campaign_id} not found")
    tenant_id = campaign.tenant_id

    external_ids = await build(session, tenant_id=tenant_id, criteria=criteria)

    # Pull the latest payload per external_id across all audiences in the
    # tenant so the snapshot carries the freshest known data per contact.
    payload_by_id: dict[str, dict[str, Any]] = {}
    if external_ids:
        stmt = (
            select(AudienceMember.external_id, AudienceMember.payload)
            .join(Audience, Audience.id == AudienceMember.audience_id)
            .where(
                Audience.tenant_id == tenant_id,
                AudienceMember.external_id.in_(external_ids),
            )
            .distinct(AudienceMember.external_id)
            .order_by(AudienceMember.external_id, AudienceMember.added_at.desc())
        )
        rows = (await session.execute(stmt)).all()
        for ext_id, payload in rows:
            payload_by_id[ext_id] = dict(payload or {})

    now = datetime.now(UTC)
    audience = Audience(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        name=audience_name,
        segment_criteria=criteria,
        estimated_size=len(external_ids),
        actual_size=len(external_ids),
        refreshed_at=now,
    )
    session.add(audience)
    await session.flush()

    for ext_id in external_ids:
        session.add(
            AudienceMember(
                audience_id=audience.id,
                external_id=ext_id,
                payload=payload_by_id.get(ext_id, {"email": ext_id}),
                source="targeted",
                fetched_at=now,
            )
        )
    if external_ids:
        await session.flush()

    return {
        "audience_id": str(audience.id),
        "campaign_id": str(campaign_id),
        "member_count": len(external_ids),
    }
