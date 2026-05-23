"""Campaign detail UI (W32, E13-S02).

Single page that renders Brief, Audience, Strategy, Content, Schedule,
plus placeholder Runs + Reports sections. Read-only — edits still flow
through the API endpoints; the UI just surfaces the state."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.ui.templates import templates
from app.db.enums import UserRole
from app.db.models import (
    AppUser,
    Audience,
    Campaign,
    CampaignReport,
    ContentAsset,
    StrategyProposal,
    StrategyTouchpoint,
)

router = APIRouter(prefix="/ui/campaigns", tags=["ui"])


@router.get("/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(
    campaign_id: UUID,
    request: Request,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    audience = (
        await db.execute(
            select(Audience)
            .where(Audience.campaign_id == campaign.id)
            .order_by(Audience.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Prefer the accepted proposal; fall back to the most recent if none yet
    # accepted — gives the marketer something to look at on a draft strategy.
    proposal = (
        await db.execute(
            select(StrategyProposal)
            .where(StrategyProposal.campaign_id == campaign.id)
            .order_by(
                StrategyProposal.is_accepted.desc(),
                StrategyProposal.version.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    content_assets = (
        await db.execute(
            select(ContentAsset)
            .where(ContentAsset.campaign_id == campaign.id)
            .order_by(
                ContentAsset.scheduled_at.asc().nullslast(),
                ContentAsset.created_at.asc(),
            )
        )
    ).scalars().all()

    touchpoints: list[StrategyTouchpoint] = []
    if proposal is not None:
        touchpoints = (
            await db.execute(
                select(StrategyTouchpoint)
                .where(StrategyTouchpoint.proposal_id == proposal.id)
                .order_by(
                    StrategyTouchpoint.scheduled_at.asc(),
                    StrategyTouchpoint.position.asc(),
                )
            )
        ).scalars().all()

    owner_label = None
    if campaign.owner_id is not None:
        owner = await db.get(AppUser, campaign.owner_id)
        owner_label = owner.display_name or owner.email if owner else None

    return templates.TemplateResponse(
        request,
        "campaigns/detail.html",
        {
            "campaign": campaign,
            "audience": audience,
            "proposal": proposal,
            "content_assets": content_assets,
            "touchpoints": touchpoints,
            "owner_label": owner_label,
        },
    )


@router.get("/{campaign_id}/report", response_class=HTMLResponse)
async def campaign_report(
    campaign_id: UUID,
    request: Request,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """W38 (E13-S04): server-rendered campaign report. Charts are a
    polish unit — this view renders tables/lists so the report is
    readable without a JS dependency."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    report = (
        await db.execute(
            select(CampaignReport).where(
                CampaignReport.campaign_id == campaign_id,
                CampaignReport.is_latest.is_(True),
            )
        )
    ).scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "campaigns/report.html",
        {"campaign": campaign, "report": report},
    )
