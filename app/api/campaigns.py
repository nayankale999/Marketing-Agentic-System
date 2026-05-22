"""Campaign endpoints (W10 baseline: CRUD + transitions).

Stories implemented in this file:
  - E03-S01  Author a campaign brief                (POST /api/campaigns)
  - E03-S02  Edit a campaign before launch          (PATCH /api/campaigns/{id})
  - E03-S06  Campaign list with filters and search  (GET  /api/campaigns)
  - E02-S05  State-machine driver (W7)              (POST /transitions/{name})

Per-channel budget allocation (E03-S04), clone (E03-S05), and lifecycle-detail
transitions (E03-S03) come with later work units when the matching downstream
systems land.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.campaign import (
    CampaignCreate,
    CampaignListResponse,
    CampaignOut,
    CampaignPatch,
    PauseCampaignRequest,
    PauseCampaignResponse,
    ResumeCampaignResponse,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot, write_audit
from app.db.enums import CampaignStatus, CampaignType, UserRole
from app.db.models import AppUser, Campaign
from app.orchestrator.state_machine import (
    GuardFailedError,
    UnknownTransitionError,
    campaign_sm,
)

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

# E03-S02: states in which a campaign may be edited via the basic PATCH path.
# Once `live`/`paused`/`completed`, edits require a manager-role flow that lands
# alongside the analytics agent in a later work unit.
_EDITABLE_STATES: frozenset[CampaignStatus] = frozenset(
    {
        CampaignStatus.drafted,
        CampaignStatus.audience_built,
        CampaignStatus.strategy_set,
        CampaignStatus.content_in_production,
        CampaignStatus.approval_pending,
        CampaignStatus.ready_to_launch,
    }
)


@router.post(
    "",
    response_model=CampaignOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_campaign(
    body: CampaignCreate,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> Campaign:
    """E03-S01 — create a campaign in `drafted` state."""
    if body.end_date < body.start_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date must be on or after start_date",
        )

    campaign = Campaign(
        tenant_id=user.tenant_id,
        owner_id=user.id,
        name=body.name,
        campaign_type=body.campaign_type,
        objective=body.objective,
        budget_total=body.budget_total,
        currency=body.currency,
        start_date=body.start_date,
        end_date=body.end_date,
        brief=body.brief,
        kpi_targets=body.kpi_targets,
    )
    db.add(campaign)
    await db.flush()
    await db.refresh(campaign)
    # `after_insert` audit listener already wrote the `campaign.created` row.
    return campaign


@router.get("", response_model=CampaignListResponse)
async def list_campaigns(
    status_filter: Annotated[CampaignStatus | None, Query(alias="status")] = None,
    campaign_type: Annotated[CampaignType | None, Query()] = None,
    q: Annotated[str | None, Query(description="case-insensitive name search")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CampaignListResponse:
    """E03-S06 — paginated list with filters + name search."""
    stmt = select(Campaign).order_by(Campaign.updated_at.desc())
    if status_filter is not None:
        stmt = stmt.where(Campaign.status == status_filter)
    if campaign_type is not None:
        stmt = stmt.where(Campaign.campaign_type == campaign_type)
    if q:
        stmt = stmt.where(Campaign.name.ilike(f"%{q}%"))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return CampaignListResponse(
        items=[CampaignOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> Campaign:
    """E03-S06 detail view — used by the API consumer + downstream agents."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")
    return campaign


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def patch_campaign(
    campaign_id: UUID,
    body: CampaignPatch,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> Campaign:
    """E03-S02 — partial update gated on lifecycle state, with an explicit
    audit_log row capturing before/after (the auto-listener only fires on
    INSERT)."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")
    if campaign.status not in _EDITABLE_STATES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"campaign in '{campaign.status.value}' state; editing the core "
                "fields is locked until it returns to a pre-launch state"
            ),
        )

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return campaign  # no-op patch

    before = column_snapshot(campaign)
    for field, value in updates.items():
        setattr(campaign, field, value)

    # Cross-field invariants after applying the patch.
    if campaign.end_date < campaign.start_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date must be on or after start_date",
        )

    # Bump updated_at explicitly because SQLAlchemy doesn't auto-touch it.
    campaign.updated_at = datetime.now(UTC)
    await db.flush()
    after = column_snapshot(campaign)

    write_audit(
        db,
        tenant_id=campaign.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="campaign",
        entity_id=campaign.id,
        action="updated",
        before_state=before,
        after_state=after,
        metadata={"fields": sorted(updates.keys())},
    )
    return campaign


@router.post("/{campaign_id}/transitions/{transition_name}")
async def apply_transition(
    campaign_id: UUID,
    transition_name: str,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict[str, str]:
    """E02-S05 — drive the campaign state machine (W7)."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")
    try:
        await campaign_sm.apply(db, campaign, transition_name)
    except UnknownTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except GuardFailedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"id": str(campaign.id), "status": campaign.status.value}


@router.post(
    "/{campaign_id}/pause",
    response_model=PauseCampaignResponse,
)
async def pause_campaign_endpoint(
    campaign_id: UUID,
    body: PauseCampaignRequest,
    _user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> PauseCampaignResponse:
    """E08-S07 #1/#2: pause a live campaign + cancel its queued tasks.

    The `pause` SM transition's on_enter handles the actual cancellation
    + audit row; this endpoint validates state and returns a summary.
    Reason ends up in the audit_log row (E08-S07 #4 hook for compliance
    auto-pauses to share the same audit shape)."""
    from sqlalchemy import func as sa_func

    from app.db.models import Task

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="campaign not found"
        )

    # Snapshot queued/awaiting count BEFORE the transition fires so we can
    # report it in the response. The transition itself does the UPDATE.
    queued_count = (
        await db.execute(
            select(sa_func.count()).select_from(Task).where(
                Task.campaign_id == campaign.id,
                Task.status.in_(["queued", "awaiting_retry"]),
            )
        )
    ).scalar_one()

    try:
        await campaign_sm.apply(db, campaign, "pause")
    except UnknownTransitionError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"campaign in '{campaign.status.value}' cannot be paused: {exc}",
        ) from exc
    except GuardFailedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Stamp the reason into a follow-up audit so the trail captures intent.
    write_audit(
        db,
        tenant_id=campaign.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="campaign",
        entity_id=campaign.id,
        action="pause_reason",
        before_state=None,
        after_state=None,
        metadata={"reason": body.reason, "cancelled_tasks": int(queued_count)},
    )

    return PauseCampaignResponse(
        campaign_id=campaign.id,
        status=campaign.status.value,
        reason=body.reason,
        cancelled_tasks=int(queued_count),
    )


@router.post(
    "/{campaign_id}/resume",
    response_model=ResumeCampaignResponse,
)
async def resume_campaign_endpoint(
    campaign_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.manager)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ResumeCampaignResponse:
    """E08-S07 #3: resume a paused campaign. The `resume` SM transition's
    on_enter re-enqueues future-slot assets and flips elapsed ones to
    failed; we surface those counts in the response."""
    from app.agents.distribution import resume_distribution_for_campaign

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="campaign not found"
        )

    if campaign.status != CampaignStatus.paused:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"campaign in '{campaign.status.value}' is not paused",
        )

    # Run the SM transition. Its on_enter calls resume_distribution_for_campaign
    # which does the requeue + elapsed-skip; we re-run it here to capture
    # the summary numbers (the on_enter is idempotent — second call is a
    # no-op since the scheduled assets have already moved on).
    try:
        await campaign_sm.apply(db, campaign, "resume")
    except (UnknownTransitionError, GuardFailedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # We need the summary. Since the on_enter already ran, the second call
    # will return {0, 0} — capture via a state-aware query against
    # content_asset and task tables instead.
    from sqlalchemy import func as sa_func

    from app.db.enums import AssetStatus
    from app.db.models import ContentAsset, Task

    requeued = (
        await db.execute(
            select(sa_func.count()).select_from(Task).where(
                Task.campaign_id == campaign.id,
                Task.status == "queued",
            )
        )
    ).scalar_one()
    elapsed = (
        await db.execute(
            select(sa_func.count()).select_from(ContentAsset).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.extra_metadata["skip_reason"].astext
                == "slot_elapsed_during_pause",
            )
        )
    ).scalar_one()

    return ResumeCampaignResponse(
        campaign_id=campaign.id,
        status=campaign.status.value,
        requeued=int(requeued),
        elapsed_failed=int(elapsed),
    )
