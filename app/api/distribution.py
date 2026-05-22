"""Distribution audit endpoints (W28, E08-S02/05).

Read-only surfaces over `dispatch_attempt` for debugging + monitoring. The
scheduling + sending paths are agent-driven via the state machine and
worker queue — no manual "send now" or "skip recipient" endpoint in W28
(those land with W31 emergency stop).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.distribution import (
    DispatchAttemptListResponse,
    DispatchAttemptOut,
)
from app.db.enums import UserRole
from app.db.models import AppUser, ContentAsset, DispatchAttempt

router = APIRouter(prefix="/api", tags=["distribution"])


@router.get(
    "/campaigns/{campaign_id}/dispatch-attempts",
    response_model=DispatchAttemptListResponse,
)
async def list_campaign_dispatch_attempts(
    campaign_id: UUID,
    asset_id: Annotated[UUID | None, Query()] = None,
    attempt_status: Annotated[
        str | None,
        Query(alias="status", description="filter by status (sent/suppressed/rejected/failed)"),
    ] = None,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> DispatchAttemptListResponse:
    """Every attempt for a campaign, joined through content_asset → campaign.
    Filterable by specific asset or status for narrower views."""
    stmt = (
        select(DispatchAttempt)
        .join(ContentAsset, ContentAsset.id == DispatchAttempt.content_asset_id)
        .where(ContentAsset.campaign_id == campaign_id)
        .order_by(DispatchAttempt.created_at.desc())
    )
    if asset_id is not None:
        stmt = stmt.where(DispatchAttempt.content_asset_id == asset_id)
    if attempt_status is not None:
        stmt = stmt.where(DispatchAttempt.status == attempt_status)
    rows = (await db.execute(stmt)).scalars().all()
    return DispatchAttemptListResponse(
        items=[DispatchAttemptOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get(
    "/dispatch-attempts/{attempt_id}",
    response_model=DispatchAttemptOut,
)
async def get_dispatch_attempt(
    attempt_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> DispatchAttempt:
    row = await db.get(DispatchAttempt, attempt_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="dispatch attempt not found"
        )
    return row
