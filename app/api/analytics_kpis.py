"""Campaign KPI dashboard endpoint (W34, E10-S01).

GET /api/campaigns/{campaign_id}/kpis returns the live rollup of every
`analytic_event` attributable to the campaign — either directly (Plausible
events whose `utm_campaign` matched at ingest) or indirectly (SendGrid
webhook events stitched back via `dispatch_attempt.provider_message_id`).

`?channel_id` and `?content_asset_id` narrow the scope so the UI can
slice the dashboard by channel or asset.

Marketer-level read is enough — this is the same role that can view
campaign detail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.kpi_rollup import compute_campaign_kpis
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.analytics_kpis import (
    CampaignKpisOut,
    CampaignKpiSnapshotOut,
    KpiDerived,
    SourceFreshnessOut,
)
from app.db.enums import UserRole
from app.db.models import AppUser, Campaign

router = APIRouter(prefix="/api/campaigns", tags=["analytics"])


@router.get(
    "/{campaign_id}/kpis",
    response_model=CampaignKpiSnapshotOut,
)
async def get_campaign_kpis(
    campaign_id: UUID,
    channel_id: Annotated[UUID | None, Query()] = None,
    content_asset_id: Annotated[UUID | None, Query()] = None,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CampaignKpiSnapshotOut:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    snapshot = await compute_campaign_kpis(
        db,
        tenant_id=user.tenant_id,
        campaign_id=campaign_id,
        channel_id=channel_id,
        content_asset_id=content_asset_id,
        now=datetime.now(UTC),
    )

    kpi_dict = snapshot.kpis.as_dict()
    return CampaignKpiSnapshotOut(
        campaign_id=snapshot.campaign_id,
        kpis=CampaignKpisOut(
            impressions=kpi_dict["impressions"],
            opens=kpi_dict["opens"],
            clicks=kpi_dict["clicks"],
            replies=kpi_dict["replies"],
            conversions=kpi_dict["conversions"],
            unsubscribes=kpi_dict["unsubscribes"],
            bounces=kpi_dict["bounces"],
            spam_complaints=kpi_dict["spam_complaints"],
            spend=kpi_dict["spend"],
            derived=KpiDerived(**kpi_dict["derived"]),
        ),
        sources=[
            SourceFreshnessOut(
                name=s.name,
                last_event_at=s.last_event_at,
                freshness_seconds=s.freshness_seconds,
                documented_latency_seconds=s.documented_latency_seconds,
            )
            for s in snapshot.sources
        ],
        generated_at=snapshot.generated_at,
    )
