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

from app.agents.strategist import (
    StrategistPreconditionError,
    assert_preconditions,
    ensure_strategist_agent,
)
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.audience import EnqueueTaskResponse
from app.api.schemas.strategy import (
    StrategyOverridePatch,
    StrategyProposalListResponse,
    StrategyProposalOut,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot, write_audit
from app.db.enums import CampaignStatus, UserRole
from app.db.models import AppUser, Campaign, StrategyProposal
from app.orchestrator.queue import enqueue_task
from app.orchestrator.state_machine import (
    GuardFailedError,
    UnknownTransitionError,
    campaign_sm,
)
from app.settings.config import get_settings

campaigns_router = APIRouter(prefix="/api/campaigns", tags=["strategy"])
proposals_router = APIRouter(prefix="/api/strategy-proposals", tags=["strategy"])


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
    """Mark this proposal as accepted and drive the campaign state machine
    from `audience_built` to `strategy_set`. The partial unique index on
    `strategy_proposal(campaign_id) WHERE is_accepted` would otherwise reject
    if a prior winner exists — we clear it in the same transaction."""
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

    before = column_snapshot(proposal)
    proposal.is_accepted = True
    await db.flush()
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
