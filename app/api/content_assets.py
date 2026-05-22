"""Content asset endpoints (W22, E06-S01/04).

  - GET   /api/campaigns/{id}/content-assets        — list + filter by status
  - GET   /api/content-assets/{id}                  — single asset detail
  - POST  /api/campaigns/{id}/content/start         — drive start_content transition
  - POST  /api/content-assets/{id}/regenerate       — re-enqueue generation for one asset

The start endpoint returns 503 when ANTHROPIC_API_KEY is missing so the
marketer sees the configuration issue at submit time rather than discovering
it only when individual generation tasks crash inside the worker.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.content_creator import (
    ContentCreatorError,
    ensure_content_creator_agent,
)
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.content_asset import (
    ContentAssetListResponse,
    ContentAssetOut,
    StartContentResponse,
)
from app.db.enums import AssetStatus, CampaignStatus, UserRole
from app.db.models import AppUser, Campaign, ContentAsset
from app.orchestrator.queue import enqueue_task
from app.orchestrator.state_machine import (
    GuardFailedError,
    UnknownTransitionError,
    campaign_sm,
)
from app.settings.config import get_settings

campaigns_router = APIRouter(prefix="/api/campaigns", tags=["content-asset"])
assets_router = APIRouter(prefix="/api/content-assets", tags=["content-asset"])


@campaigns_router.get(
    "/{campaign_id}/content-assets",
    response_model=ContentAssetListResponse,
)
async def list_content_assets(
    campaign_id: UUID,
    asset_status: Annotated[
        AssetStatus | None, Query(alias="status", description="filter by status")
    ] = None,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ContentAssetListResponse:
    """E06-S01 #1: 'reflected in the asset list' — this is that list."""
    stmt = (
        select(ContentAsset)
        .where(ContentAsset.campaign_id == campaign_id)
        .order_by(
            ContentAsset.scheduled_at.asc().nullslast(),
            ContentAsset.created_at.asc(),
        )
    )
    if asset_status is not None:
        stmt = stmt.where(ContentAsset.status == asset_status)
    rows = (await db.execute(stmt)).scalars().all()
    return ContentAssetListResponse(
        items=[ContentAssetOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@assets_router.get("/{asset_id}", response_model=ContentAssetOut)
async def get_content_asset(
    asset_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ContentAsset:
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")
    return asset


@campaigns_router.post(
    "/{campaign_id}/content/start",
    response_model=StartContentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_content_production(
    campaign_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> StartContentResponse:
    """E06-S01 #1: drive the `start_content` transition. Generation tasks are
    enqueued inside the transition's on_enter; the response counts how many
    assets the seed produced."""
    if not get_settings().anthropic_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="content creator is not configured (ANTHROPIC_API_KEY missing)",
        )

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    try:
        await campaign_sm.apply(db, campaign, "start_content")
    except UnknownTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except GuardFailedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ContentCreatorError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    assets_count = (
        await db.execute(
            select(func.count()).select_from(ContentAsset).where(
                ContentAsset.campaign_id == campaign.id
            )
        )
    ).scalar_one()
    return StartContentResponse(
        campaign_id=campaign.id,
        status=campaign.status.value,
        assets_created=int(assets_count),
    )


@assets_router.post(
    "/{asset_id}/regenerate",
    response_model=ContentAssetOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_asset(
    asset_id: UUID,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ContentAsset:
    """E06-S01 #3: re-enqueue generation for a failed/drafted asset. Flips
    the row back to `requested` so the worker treats it as fresh work."""
    if not get_settings().anthropic_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="content creator is not configured (ANTHROPIC_API_KEY missing)",
        )

    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")

    campaign = await db.get(Campaign, asset.campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")
    if campaign.status not in {
        CampaignStatus.content_in_production,
        CampaignStatus.approval_pending,
    }:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"cannot regenerate assets while campaign is in "
                f"'{campaign.status.value}'"
            ),
        )

    asset.status = AssetStatus.requested
    await db.flush()

    agent = await ensure_content_creator_agent(db, asset.tenant_id)
    await enqueue_task(
        db,
        tenant_id=asset.tenant_id,
        agent_id=agent.id,
        campaign_id=asset.campaign_id,
        skill_name="content_creator.generate_asset",
        input_data={
            "asset_id": str(asset.id),
            "campaign_id": str(asset.campaign_id),
            "triggered_by_user_id": str(user.id),
        },
    )
    return asset
