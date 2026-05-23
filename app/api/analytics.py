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

    # W37 ships the audit + status marker only. The actual mutation of
    # strategy_proposal.payload (for budget_shift) is a manager-facing
    # follow-up — the recommendation row itself plus the audit_log entry
    # is the durable record of "operator accepted this change."
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
