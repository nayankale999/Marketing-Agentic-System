"""Analytics agent endpoints (W37, E10-S02 / E10-S03).

  - GET   /api/campaigns/{id}/anomalies            list anomalies
  - POST  /api/anomalies/{id}/dismiss              admin-only silence
  - GET   /api/campaigns/{id}/recommendations      list (uplift filter)
  - POST  /api/recommendations/{id}/accept         apply + record
  - POST  /api/recommendations/{id}/reject         status -> rejected
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.anomaly import dismiss_anomaly
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.analytics import (
    MetricAnomalyListResponse,
    MetricAnomalyOut,
    OptimisationRecommendationOut,
    RecommendationListResponse,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import UserRole
from app.db.models import (
    AppUser,
    Campaign,
    CampaignChannelBudget,
    MetricAnomaly,
    OptimisationRecommendation,
)


anomalies_router = APIRouter(prefix="/api", tags=["analytics"])


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


@anomalies_router.get(
    "/campaigns/{campaign_id}/anomalies",
    response_model=MetricAnomalyListResponse,
)
async def list_anomalies(
    campaign_id: UUID,
    include_dismissed: Annotated[bool, Query()] = False,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> MetricAnomalyListResponse:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    stmt = select(MetricAnomaly).where(MetricAnomaly.campaign_id == campaign_id)
    if not include_dismissed:
        stmt = stmt.where(MetricAnomaly.dismissed_at.is_(None))
    stmt = stmt.order_by(MetricAnomaly.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return MetricAnomalyListResponse(
        items=[MetricAnomalyOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@anomalies_router.post(
    "/anomalies/{anomaly_id}/dismiss",
    response_model=MetricAnomalyOut,
)
async def dismiss_anomaly_endpoint(
    anomaly_id: UUID,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> MetricAnomalyOut:
    """E10-S02 AC #4: admin-only. Silences the anomaly for 24h."""
    anomaly = await db.get(MetricAnomaly, anomaly_id)
    if anomaly is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="anomaly not found")
    if anomaly.dismissed_at is not None:
        return MetricAnomalyOut.model_validate(anomaly)
    row = await dismiss_anomaly(
        db,
        anomaly_id=anomaly_id,
        dismissed_by=user.id,
        now=datetime.now(UTC),
    )
    return MetricAnomalyOut.model_validate(row)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@anomalies_router.get(
    "/campaigns/{campaign_id}/recommendations",
    response_model=RecommendationListResponse,
)
async def list_recommendations(
    campaign_id: UUID,
    min_uplift: Annotated[float, Query(ge=0, le=1)] = 0.05,
    include_low_uplift: Annotated[bool, Query()] = False,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> RecommendationListResponse:
    """E10-S03 AC #4: below-threshold uplift hidden unless asked for."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    stmt = select(OptimisationRecommendation).where(
        OptimisationRecommendation.campaign_id == campaign_id,
    )
    if not include_low_uplift:
        # NULL uplift is treated as "always show" since the rule doesn't
        # mandate a value; only rules that opt in fill the column.
        stmt = stmt.where(
            (OptimisationRecommendation.predicted_uplift.is_(None))
            | (OptimisationRecommendation.predicted_uplift >= Decimal(str(min_uplift)))
        )
    stmt = stmt.order_by(OptimisationRecommendation.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return RecommendationListResponse(
        items=[OptimisationRecommendationOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@anomalies_router.post(
    "/recommendations/{recommendation_id}/accept",
    response_model=OptimisationRecommendationOut,
)
async def accept_recommendation(
    recommendation_id: UUID,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> OptimisationRecommendationOut:
    rec = await db.get(OptimisationRecommendation, recommendation_id)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="recommendation not found")
    if rec.status != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"recommendation is in status '{rec.status}'",
        )

    # W39 (E10-S05 AC #3): for budget_shift, apply the new allocations to
    # `campaign_channel_budget` so the next dispatch wave reads the new
    # plan. Other recommendation kinds are still marker-only.
    if rec.kind == "budget_shift":
        await _apply_budget_shift(db, rec=rec)

    rec.status = "applied"
    rec.applied_at = datetime.now(UTC)
    rec.applied_by = user.id

    write_audit(
        db,
        tenant_id=rec.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="optimisation_recommendation",
        entity_id=rec.id,
        action="recommendation_accepted",
        before_state=None,
        after_state=None,
        metadata={"kind": rec.kind, "predicted_uplift": str(rec.predicted_uplift)},
    )
    await db.flush()
    return OptimisationRecommendationOut.model_validate(rec)


async def _apply_budget_shift(
    db: AsyncSession, *, rec: OptimisationRecommendation
) -> None:
    """E10-S05 AC #3: upsert `campaign_channel_budget` rows so the next
    dispatch wave reads the post-shift allocation.

    Idempotent — the recommendation lifecycle (only `pending → applied`)
    prevents double-application, but the upsert is safe to re-run."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    proposal = rec.proposal or {}
    from_payload = proposal.get("from") or {}
    to_payload = proposal.get("to") or {}
    from_channel_id = from_payload.get("channel_id")
    to_channel_id = to_payload.get("channel_id")
    # Legacy W37 proposals (and any future kind without channel_ids) get
    # the marker-only treatment — we still flip status to `applied` but
    # don't touch campaign_channel_budget.
    if not from_channel_id or not to_channel_id:
        return
    try:
        from_id = UUID(from_channel_id)
        to_id = UUID(to_channel_id)
    except (TypeError, ValueError):
        return

    new_from = Decimal(str(from_payload.get("new_allocation_amount") or "0"))
    new_to = Decimal(str(to_payload.get("new_allocation_amount") or "0"))

    for channel_id, allocated in ((from_id, new_from), (to_id, new_to)):
        stmt = (
            pg_insert(CampaignChannelBudget)
            .values(
                campaign_id=rec.campaign_id,
                channel_id=channel_id,
                allocated=allocated,
            )
            .on_conflict_do_update(
                index_elements=[
                    CampaignChannelBudget.campaign_id,
                    CampaignChannelBudget.channel_id,
                ],
                set_={"allocated": allocated},
            )
        )
        await db.execute(stmt)


@anomalies_router.post(
    "/recommendations/{recommendation_id}/reject",
    response_model=OptimisationRecommendationOut,
)
async def reject_recommendation(
    recommendation_id: UUID,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> OptimisationRecommendationOut:
    rec = await db.get(OptimisationRecommendation, recommendation_id)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="recommendation not found")
    if rec.status != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"recommendation is in status '{rec.status}'",
        )
    rec.status = "rejected"
    write_audit(
        db,
        tenant_id=rec.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="optimisation_recommendation",
        entity_id=rec.id,
        action="recommendation_rejected",
        before_state=None,
        after_state=None,
        metadata={"kind": rec.kind},
    )
    await db.flush()
    return OptimisationRecommendationOut.model_validate(rec)
