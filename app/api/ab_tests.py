"""A/B test endpoints (W23, E06-S05; W35, E09-S01/02).

  - GET   /api/campaigns/{id}/ab-tests          — list tests for a campaign
  - POST  /api/campaigns/{id}/ab-tests          — create a new A/B test (W35)
  - GET   /api/ab-tests/{id}                    — detail including every linked variant id
  - POST  /api/ab-tests/{id}/add-variant        — fan out one more variant, up to MAX_VARIANTS
  - POST  /api/ab-tests/{id}/launch             — designing → running (W35)
  - POST  /api/ab-tests/{id}/stop               — running → stopped (W35)

Variant assets are joined back to their parent ab_test via
`content_asset.extra_metadata.ab_test_group_id` so the multivariate case
(>2 variants per AC #3) doesn't need a schema change.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._variants import MAX_VARIANTS, angle_for_index
from app.agents.content_creator import ensure_content_creator_agent
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.ab_test import (
    AbTestDetail,
    AbTestListResponse,
    AbTestOut,
    AddVariantResponse,
    CreateAbTestRequest,
)
from app.db.enums import AbTestStatus, AssetStatus, AssetType, UserRole
from app.db.models import AbTest, AppUser, Campaign, ContentAsset
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


# ---------------------------------------------------------------------------
# W35 — define / launch / stop
# ---------------------------------------------------------------------------


# Asset statuses that prove the variant has cleared review and is safe to
# launch on. We accept both `approved` (just approved, not yet scheduled by
# the distribution agent) and `scheduled` (auto-advanced — see W28).
_LAUNCH_READY_STATUSES = {AssetStatus.approved, AssetStatus.scheduled}


@campaigns_router.post(
    "/{campaign_id}/ab-tests",
    response_model=AbTestOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_ab_test(
    campaign_id: UUID,
    body: CreateAbTestRequest,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AbTestOut:
    """W35 (E09-S01): define an A/B test on existing variants.

    Validates split sums to 100, every variant belongs to this campaign,
    no duplicate variant ids. Rejects if there is already an active
    (`designing`/`running`) test on the same variant_a — the partial
    unique index in migration 0018 backstops this at the DB level too."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    if len(set(body.variant_ids)) != len(body.variant_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="duplicate variant ids in request",
        )

    split_total = sum(body.traffic_split.values())
    if split_total != 100:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"traffic_split must sum to 100, got {split_total}",
        )
    if set(body.traffic_split.keys()) != set(body.variant_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="traffic_split keys must match variant_ids exactly",
        )

    variants = (
        await db.execute(
            select(ContentAsset).where(
                ContentAsset.id.in_(body.variant_ids),
                ContentAsset.tenant_id == user.tenant_id,
            )
        )
    ).scalars().all()
    if len(variants) != len(body.variant_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="one or more variants not found",
        )
    for v in variants:
        if v.campaign_id != campaign_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"variant {v.id} does not belong to campaign {campaign_id}",
            )

    ab_test = AbTest(
        tenant_id=user.tenant_id,
        campaign_id=campaign_id,
        name=body.name,
        hypothesis=body.hypothesis,
        primary_metric=body.primary_metric,
        status=AbTestStatus.designing,
        variant_a_id=body.variant_ids[0],
        variant_b_id=body.variant_ids[1],
        traffic_split={str(k): int(v) for k, v in body.traffic_split.items()},
        min_runtime_hours=body.min_runtime_hours,
        max_runtime_hours=body.max_runtime_hours,
    )
    db.add(ab_test)
    try:
        await db.flush()
    except IntegrityError as exc:
        # uq_ab_test_active_per_variant_a — second active test on the
        # same family.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="an active A/B test already exists on this asset family",
        ) from exc

    # Link any 'extra' variants (3rd+) via extra_metadata so existing
    # _list_variants still finds them.
    for variant in variants:
        meta = dict(variant.extra_metadata or {})
        meta["ab_test_group_id"] = str(ab_test.id)
        variant.extra_metadata = meta
    await db.flush()

    return AbTestOut.model_validate(ab_test)


@ab_tests_router.post(
    "/{ab_test_id}/launch",
    response_model=AbTestOut,
)
async def launch_ab_test(
    ab_test_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AbTestOut:
    """W35 (E09-S01 AC #4): block launch until every variant is approved.

    The asset family progresses through `pending_approval` (E07) →
    `approved` → `scheduled` (E08). Either of the last two is fine; the
    earlier two are not."""
    ab_test = await db.get(AbTest, ab_test_id)
    if ab_test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ab_test not found")
    if ab_test.status != AbTestStatus.designing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"ab_test is in status '{ab_test.status.value}', expected 'designing'",
        )

    variants = await _list_variants(db, ab_test_id=ab_test_id)
    # Fallback when the family was never registered via extra_metadata
    # (older tests, or this one before add-variant fan-out).
    if not variants:
        ids = [ab_test.variant_a_id, ab_test.variant_b_id]
        ids = [i for i in ids if i is not None]
        if ids:
            variants = list(
                (
                    await db.execute(
                        select(ContentAsset).where(ContentAsset.id.in_(ids))
                    )
                ).scalars().all()
            )

    if not variants:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="ab_test has no variants to launch",
        )

    not_ready = [v for v in variants if v.status not in _LAUNCH_READY_STATUSES]
    if not_ready:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "variants_not_approved",
                "variant_ids": [str(v.id) for v in not_ready],
                "statuses": [v.status.value for v in not_ready],
            },
        )

    ab_test.status = AbTestStatus.running
    ab_test.started_at = datetime.now(UTC)
    await db.flush()
    return AbTestOut.model_validate(ab_test)


@ab_tests_router.post(
    "/{ab_test_id}/stop",
    response_model=AbTestOut,
)
async def stop_ab_test(
    ab_test_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AbTestOut:
    """W35 (E09-S03 AC #4 trailer): manual stop sets status without auto-
    setting a winner, even if one arm is numerically ahead."""
    ab_test = await db.get(AbTest, ab_test_id)
    if ab_test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ab_test not found")
    if ab_test.status not in {AbTestStatus.designing, AbTestStatus.running}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"ab_test is in status '{ab_test.status.value}'",
        )

    ab_test.status = AbTestStatus.stopped
    ab_test.stopped_at = datetime.now(UTC)
    await db.flush()
    return AbTestOut.model_validate(ab_test)


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
