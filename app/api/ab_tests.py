"""A/B test endpoints (W23, E06-S05).

  - GET   /api/campaigns/{id}/ab-tests          — list tests for a campaign
  - GET   /api/ab-tests/{id}                    — detail including every linked variant id
  - POST  /api/ab-tests/{id}/add-variant        — fan out one more variant, up to MAX_VARIANTS

Variant assets are joined back to their parent ab_test via
`content_asset.extra_metadata.ab_test_group_id` so the multivariate case
(>2 variants per AC #3) doesn't need a schema change.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._variants import MAX_VARIANTS, angle_for_index
from app.agents.content_creator import ensure_content_creator_agent
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.ab_test import (
    AbTestDetail,
    AbTestListResponse,
    AbTestOut,
    AddVariantResponse,
)
from app.db.enums import AssetStatus, AssetType, UserRole
from app.db.models import AbTest, AppUser, ContentAsset
from app.orchestrator.queue import enqueue_task
from app.settings.config import get_settings

campaigns_router = APIRouter(prefix="/api/campaigns", tags=["ab-test"])
ab_tests_router = APIRouter(prefix="/api/ab-tests", tags=["ab-test"])


@campaigns_router.get(
    "/{campaign_id}/ab-tests",
    response_model=AbTestListResponse,
)
async def list_ab_tests(
    campaign_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AbTestListResponse:
    rows = (
        await db.execute(
            select(AbTest)
            .where(AbTest.campaign_id == campaign_id)
            .order_by(AbTest.created_at.asc())
        )
    ).scalars().all()
    return AbTestListResponse(
        items=[AbTestOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@ab_tests_router.get("/{ab_test_id}", response_model=AbTestDetail)
async def get_ab_test(
    ab_test_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AbTestDetail:
    ab_test = await db.get(AbTest, ab_test_id)
    if ab_test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ab_test not found")

    variant_ids = await _list_variant_ids(db, ab_test_id=ab_test_id)
    return AbTestDetail(
        **AbTestOut.model_validate(ab_test).model_dump(),
        variant_ids=variant_ids,
    )


@ab_tests_router.post(
    "/{ab_test_id}/add-variant",
    response_model=AddVariantResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def add_variant(
    ab_test_id: UUID,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AddVariantResponse:
    """E06-S05 #3: add another variant to an existing A/B test, up to
    MAX_VARIANTS total. Re-uses the first variant's touchpoint metadata so
    the new asset lands on the same channel/schedule.
    """
    if not get_settings().anthropic_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="content creator is not configured (ANTHROPIC_API_KEY missing)",
        )

    ab_test = await db.get(AbTest, ab_test_id)
    if ab_test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ab_test not found")

    existing = await _list_variants(db, ab_test_id=ab_test_id)
    if not existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="ab_test has no existing variants to clone from",
        )
    if len(existing) >= MAX_VARIANTS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"ab_test already has the maximum of {MAX_VARIANTS} variants",
        )

    next_index = max(
        int(v.extra_metadata.get("variant_index", 0)) for v in existing
    ) + 1
    template = existing[0]
    new_asset = ContentAsset(
        tenant_id=template.tenant_id,
        campaign_id=template.campaign_id,
        channel_id=template.channel_id,
        asset_type=template.asset_type,
        status=AssetStatus.requested,
        is_required=False,  # additional variants are exploratory, not blocking
        scheduled_at=template.scheduled_at,
        extra_metadata={
            "touchpoint_id": template.extra_metadata.get("touchpoint_id"),
            "channel_platform": template.extra_metadata.get("channel_platform"),
            "ab_test_group_id": str(ab_test_id),
            "variant_index": next_index,
            "variant_angle": angle_for_index(next_index),
            "is_baseline": False,
        },
    )
    db.add(new_asset)
    await db.flush()

    agent = await ensure_content_creator_agent(db, new_asset.tenant_id)
    task = await enqueue_task(
        db,
        tenant_id=new_asset.tenant_id,
        agent_id=agent.id,
        campaign_id=new_asset.campaign_id,
        skill_name="content_creator.generate_asset",
        input_data={
            "asset_id": str(new_asset.id),
            "campaign_id": str(new_asset.campaign_id),
            "triggered_by_user_id": str(user.id),
        },
    )
    return AddVariantResponse(
        ab_test_id=ab_test_id,
        variant_id=new_asset.id,
        variant_index=next_index,
        task_id=task.id,
    )


async def _list_variants(
    db: AsyncSession, *, ab_test_id: UUID
) -> list[ContentAsset]:
    rows = (
        await db.execute(
            select(ContentAsset).where(
                ContentAsset.extra_metadata["ab_test_group_id"].astext == str(ab_test_id),
            )
        )
    ).scalars().all()
    return rows


async def _list_variant_ids(db: AsyncSession, *, ab_test_id: UUID) -> list[UUID]:
    rows = await _list_variants(db, ab_test_id=ab_test_id)
    return [r.id for r in rows]
