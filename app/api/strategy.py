"""Campaign strategy endpoints (W20, E05-S01/02).

  - POST  /api/campaigns/{id}/strategy           — enqueue a propose task (E05-S01 #1)
  - GET   /api/campaigns/{id}/strategy           — latest proposal
  - GET   /api/campaigns/{id}/strategy/history   — every version, newest first (E05-S01 #3)
  - PATCH /api/strategy-proposals/{id}           — apply human overrides (E05-S02 #1)
  - POST  /api/strategy-proposals/{id}/accept    — accept + transition state

Accepted proposals drive the `audience_built -> strategy_set` state machine
transition; the accept endpoint flips the flag and then applies the transition
in the same request so callers don't have to chain two API calls.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._calendar import HardCapViolationError
from app.agents.strategist import (
    CalendarSeedError,
    StrategistPreconditionError,
    assert_preconditions,
    ensure_strategist_agent,
    re_evaluate_warnings,
    seed_calendar,
)
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.audience import EnqueueTaskResponse
from app.api.schemas.strategy import (
    CalendarResponse,
    StrategyOverridePatch,
    StrategyProposalListResponse,
    StrategyProposalOut,
    TouchpointOut,
    TouchpointPatch,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot, write_audit
from app.db.enums import CampaignStatus, UserRole
from app.db.models import AppUser, Campaign, StrategyProposal, StrategyTouchpoint
from app.orchestrator.queue import enqueue_task
from app.orchestrator.state_machine import (
    GuardFailedError,
    UnknownTransitionError,
    campaign_sm,
)
from app.settings.config import get_settings

campaigns_router = APIRouter(prefix="/api/campaigns", tags=["strategy"])
proposals_router = APIRouter(prefix="/api/strategy-proposals", tags=["strategy"])
touchpoints_router = APIRouter(prefix="/api/strategy-touchpoints", tags=["strategy"])


@campaigns_router.post(
    "/{campaign_id}/strategy",
    response_model=EnqueueTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_strategy_propose(
    campaign_id: UUID,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> EnqueueTaskResponse:
    """E05-S01 #1: enqueue the propose task. Precondition checks run first
    (E05-S01 #4) so the marketer sees a 422 instead of a buried task failure."""
    if not get_settings().anthropic_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="campaign strategist is not configured (ANTHROPIC_API_KEY missing)",
        )

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    try:
        await assert_preconditions(db, campaign)
    except StrategistPreconditionError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    agent = await ensure_strategist_agent(db, user.tenant_id)
    task = await enqueue_task(
        db,
        tenant_id=user.tenant_id,
        agent_id=agent.id,
        campaign_id=campaign_id,
        skill_name="campaign_strategist.propose",
        input_data={
            "campaign_id": str(campaign_id),
            "triggered_by_user_id": str(user.id),
        },
    )
    return EnqueueTaskResponse(
        task_id=task.id,
        skill_name=task.skill_name,
        status=task.status.value,
    )


@campaigns_router.get(
    "/{campaign_id}/strategy",
    response_model=StrategyProposalOut,
)
async def get_latest_strategy(
    campaign_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> StrategyProposal:
    """Return the highest-version proposal for the campaign, or 404 if none."""
    row = (
        await db.execute(
            select(StrategyProposal)
            .where(StrategyProposal.campaign_id == campaign_id)
            .order_by(StrategyProposal.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no strategy proposal exists for this campaign",
        )
    return row


@campaigns_router.get(
    "/{campaign_id}/strategy/history",
    response_model=StrategyProposalListResponse,
)
async def list_strategy_history(
    campaign_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> StrategyProposalListResponse:
    rows = (
        await db.execute(
            select(StrategyProposal)
            .where(StrategyProposal.campaign_id == campaign_id)
            .order_by(StrategyProposal.version.desc())
        )
    ).scalars().all()
    return StrategyProposalListResponse(
        items=[StrategyProposalOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@proposals_router.patch("/{proposal_id}", response_model=StrategyProposalOut)
async def patch_strategy_overrides(
    proposal_id: UUID,
    body: StrategyOverridePatch,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> StrategyProposal:
    """E05-S02 #1: stamp `human_override` on the affected channel rows of the
    proposal payload. Mutates the JSONB in place; the re-plan path reads these
    on the next propose call (`_carry_overrides` in app.agents.strategist).

    Accepted proposals are immutable — overrides must be applied before accept.
    """
    proposal = await db.get(StrategyProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="proposal not found")
    if proposal.is_accepted:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="accepted proposals are immutable; re-propose to amend",
        )

    payload = dict(proposal.payload)
    channels = list(payload.get("channels", []))
    channels_by_platform = {
        str(ch.get("platform", "")).lower(): i
        for i, ch in enumerate(channels)
        if isinstance(ch, dict)
    }

    changed: list[str] = []
    for override in body.channel_overrides:
        platform = override.platform.lower()
        idx = channels_by_platform.get(platform)
        if idx is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"platform '{override.platform}' is not in this proposal",
            )
        ch = dict(channels[idx])
        if override.allocation_pct is not None:
            ch["allocation_pct"] = override.allocation_pct
        if override.allocation_amount is not None:
            try:
                Decimal(override.allocation_amount)
            except InvalidOperation as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"allocation_amount '{override.allocation_amount}' is not numeric",
                ) from exc
            ch["allocation_amount"] = override.allocation_amount
        ch["human_override"] = override.human_override
        channels[idx] = ch
        changed.append(platform)

    payload["channels"] = channels
    before = column_snapshot(proposal)
    proposal.payload = payload
    await db.flush()
    after = column_snapshot(proposal)

    write_audit(
        db,
        tenant_id=proposal.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="strategy_proposal",
        entity_id=proposal.id,
        action="overridden",
        before_state=before,
        after_state=after,
        metadata={"channels": changed},
    )
    return proposal


@proposals_router.post("/{proposal_id}/accept", response_model=StrategyProposalOut)
async def accept_strategy(
    proposal_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> StrategyProposal:
    """Mark this proposal as accepted, seed the touchpoint calendar (W21,
    E05-S03), and drive the campaign state machine from `audience_built` to
    `strategy_set`. The partial unique index on
    `strategy_proposal(campaign_id) WHERE is_accepted` would otherwise reject
    if a prior winner exists — we clear it in the same transaction.

    If the calendar can't be generated under current hard caps (E05-S05 #2),
    the whole accept is rolled back so the campaign doesn't half-transition.
    """
    proposal = await db.get(StrategyProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="proposal not found")

    if proposal.is_accepted:
        return proposal  # idempotent

    campaign = await db.get(Campaign, proposal.campaign_id)
    if campaign is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="campaign not found"
        )

    # Clear any prior accepted proposal for this campaign first.
    await db.execute(
        update(StrategyProposal)
        .where(
            StrategyProposal.campaign_id == proposal.campaign_id,
            StrategyProposal.is_accepted.is_(True),
            StrategyProposal.id != proposal.id,
        )
        .values(is_accepted=False)
    )
    # Drop the old calendar — touchpoints are per-proposal and a new accept
    # supersedes any previously-seeded set.
    await db.execute(
        StrategyTouchpoint.__table__.delete().where(
            StrategyTouchpoint.proposal_id.in_(
                select(StrategyProposal.id).where(
                    StrategyProposal.campaign_id == proposal.campaign_id,
                    StrategyProposal.id != proposal.id,
                )
            )
        )
    )

    before = column_snapshot(proposal)
    proposal.is_accepted = True
    await db.flush()

    try:
        await seed_calendar(db, proposal=proposal)
    except CalendarSeedError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except HardCapViolationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "hard cap violation",
                "violations": exc.violations,
            },
        ) from exc

    after = column_snapshot(proposal)

    write_audit(
        db,
        tenant_id=proposal.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="strategy_proposal",
        entity_id=proposal.id,
        action="accepted",
        before_state=before,
        after_state=after,
        metadata={"campaign_id": str(proposal.campaign_id), "version": proposal.version},
    )

    # Drive the state machine forward. Only attempt the transition if the
    # campaign is still in `audience_built` — accept-after-launch shouldn't
    # silently roll the state back.
    if campaign.status == CampaignStatus.audience_built:
        try:
            await campaign_sm.apply(db, campaign, "set_strategy")
        except (UnknownTransitionError, GuardFailedError) as exc:
            # Shouldn't happen — we just flipped the accepted flag — but surface
            # cleanly if state changed under us between the flush and the apply.
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return proposal


@campaigns_router.get(
    "/{campaign_id}/strategy/calendar",
    response_model=CalendarResponse,
)
async def get_campaign_calendar(
    campaign_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CalendarResponse:
    """E05-S03 #1: every planned send/post across channels, sorted by time."""
    accepted = (
        await db.execute(
            select(StrategyProposal)
            .where(
                StrategyProposal.campaign_id == campaign_id,
                StrategyProposal.is_accepted.is_(True),
            )
        )
    ).scalar_one_or_none()
    if accepted is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no accepted strategy proposal for this campaign",
        )

    rows = (
        await db.execute(
            select(StrategyTouchpoint)
            .where(StrategyTouchpoint.proposal_id == accepted.id)
            .order_by(
                StrategyTouchpoint.scheduled_at.asc(),
                StrategyTouchpoint.position.asc(),
            )
        )
    ).scalars().all()
    return CalendarResponse(
        proposal_id=accepted.id,
        items=[TouchpointOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@touchpoints_router.patch("/{touchpoint_id}", response_model=TouchpointOut)
async def patch_touchpoint(
    touchpoint_id: UUID,
    body: TouchpointPatch,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> StrategyTouchpoint:
    """E05-S03 #3 + E05-S05 #2: drag a touchpoint to a new date. Rejects 422
    if the move would violate a tenant hard cap; re-evaluates frequency
    warnings for the rest of the proposal's calendar; bumps the parent
    proposal's `updated_at` per the W21 Option B trade-off (in-place edit
    rather than full proposal clone)."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.agents._calendar import (
        PlannedTouchpoint,
        enforce_hard_caps,
    )
    from app.db.enums import TenantConstraintKind
    from app.db.models import TenantConstraint

    touchpoint = await db.get(StrategyTouchpoint, touchpoint_id)
    if touchpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="touchpoint not found")

    proposal = await db.get(StrategyProposal, touchpoint.proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="parent proposal missing")

    before = column_snapshot(touchpoint)
    touchpoint.scheduled_at = body.scheduled_at
    touchpoint.human_override = body.human_override

    # Re-validate hard caps against the candidate full set (post-move).
    siblings = (
        await db.execute(
            select(StrategyTouchpoint).where(
                StrategyTouchpoint.proposal_id == touchpoint.proposal_id
            )
        )
    ).scalars().all()
    planned = [
        PlannedTouchpoint(
            channel_platform=row.channel_platform,
            audience_id=row.audience_id,
            scheduled_at=row.scheduled_at,
            position=row.position,
            human_override=row.human_override,
        )
        for row in siblings
    ]
    constraints = (
        await db.execute(
            select(TenantConstraint).where(
                TenantConstraint.tenant_id == proposal.tenant_id
            )
        )
    ).scalars().all()
    hard_caps = [
        dict(c.payload)
        for c in constraints
        if c.kind == TenantConstraintKind.hard_cap.value
    ]
    try:
        enforce_hard_caps(planned, hard_caps)
    except HardCapViolationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "hard cap violation", "violations": exc.violations},
        ) from exc

    await re_evaluate_warnings(db, proposal_id=touchpoint.proposal_id)
    proposal.updated_at = _dt.now(_UTC)
    await db.flush()
    after = column_snapshot(touchpoint)

    write_audit(
        db,
        tenant_id=touchpoint.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="strategy_touchpoint",
        entity_id=touchpoint.id,
        action="moved",
        before_state=before,
        after_state=after,
        metadata={"proposal_id": str(touchpoint.proposal_id)},
    )
    return touchpoint
