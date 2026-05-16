"""Campaign endpoints (W7 baseline: just the transition driver).

Full CRUD lands in W10 (Slice 2). For Slice 1 we expose only the state-machine
driver so the orchestrator -> queue -> worker loop can be exercised end-to-end.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.db.enums import UserRole
from app.db.models import AppUser, Campaign
from app.orchestrator.state_machine import (
    GuardFailedError,
    UnknownTransitionError,
    campaign_sm,
)

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.post("/{campaign_id}/transitions/{transition_name}")
async def apply_transition(
    campaign_id: UUID,
    transition_name: str,
    _user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict[str, str]:
    """Apply a named transition to the campaign and return the new status."""
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
