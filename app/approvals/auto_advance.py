"""Drive a campaign forward after an individual asset is approved.

Both the assistant `approve_asset` tool and the UI POST handler call
this — they share the exact same semantics so the two paths can't
diverge. Steps:

  1. If the campaign is still in `content_in_production`, attempt
     `submit_for_approval`. The guard requires that no required asset
     is in a pre-drafted state; the on_enter hook flips remaining
     drafted assets to `pending_approval`.
  2. If the campaign is in `approval_pending` (either it already was,
     or step 1 just moved it there), attempt `start_launch`. The
     guard checks every required asset is approved. The on_enter
     hook schedules dispatch tasks.

Both transitions are wrapped in try/except — a failed guard is the
normal "more work to do" path, not an error. We return the campaign's
final state so the caller can produce a useful user-facing message.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import CampaignStatus
from app.db.models import Campaign


async def try_advance_after_approval(
    session: AsyncSession, campaign: Campaign
) -> str | None:
    """Returns 'ready_to_launch' if the campaign advanced all the way
    to launch-ready, otherwise None. The campaign object is mutated
    in place — caller does not need to refresh."""
    from app.orchestrator.state_machine import (
        GuardFailedError,
        UnknownTransitionError,
        campaign_sm,
    )

    if campaign.status == CampaignStatus.content_in_production:
        try:
            await campaign_sm.apply(session, campaign, "submit_for_approval")
        except (UnknownTransitionError, GuardFailedError):
            return None
    if campaign.status == CampaignStatus.approval_pending:
        try:
            await campaign_sm.apply(session, campaign, "start_launch")
        except (UnknownTransitionError, GuardFailedError):
            return None
    return (
        campaign.status.value
        if campaign.status == CampaignStatus.ready_to_launch
        else None
    )


__all__ = ["try_advance_after_approval"]
