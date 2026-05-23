"""Custom KPI + spend reconciliation endpoints (W41, E10-S07 / E10-S06).

Custom KPIs:
  - POST   /api/custom-kpis                            (marketer)
  - GET    /api/custom-kpis                            (viewer)
  - DELETE /api/custom-kpis/{id}                       soft delete (marketer)
  - GET    /api/campaigns/{id}/custom-kpis             evaluated (viewer)

Spend reconciliation:
  - POST   /api/spend-reconciliation/run               (admin)
  - GET    /api/campaigns/{id}/reconciliation          (viewer)
  - POST   /api/spend-reconciliation/{id}/explain      (admin)
  - POST   /api/spend-reconciliation/{id}/dispute      (admin)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.custom_kpis import evaluate_custom_kpi
from app.analytics.spend_reconciliation import (
    mark_disputed,
    mark_explained,
    run_reconciliation,
)
from app.api.deps import get_tenant_db, require_role
from app.api.schemas.custom_kpis import (
    CreateCustomKpiRequest,
    CustomKpiEvaluation,
    CustomKpiEvaluationListResponse,
    CustomKpiListResponse,
    CustomKpiOut,
    DisputeReconciliationRequest,
    ExplainReconciliationRequest,
    RunReconciliationRequest,
    SpendReconciliationListResponse,
    SpendReconciliationOut,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import UserRole
from app.db.models import (
    AppUser,
    Campaign,
    CustomKpi,
    SpendReconciliation,
)


router = APIRouter(prefix="/api", tags=["analytics"])


# ---------------------------------------------------------------------------
# Custom KPI CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/custom-kpis",
    response_model=CustomKpiOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_kpi(
    body: CreateCustomKpiRequest,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CustomKpiOut:
    if body.campaign_id is not None:
        campaign = await db.get(Campaign, body.campaign_id)
        if campaign is None or campaign.tenant_id != user.tenant_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="campaign not found",
            )

    if not body.formula.get("event_type"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="formula.event_type is required",
        )

    row = CustomKpi(
        tenant_id=user.tenant_id,
        campaign_id=body.campaign_id,
        name=body.name,
        formula=body.formula,
        created_by=user.id,
    )
    db.add(row)
    await db.flush()
    return CustomKpiOut.model_validate(row)


@router.get("/custom-kpis", response_model=CustomKpiListResponse)
async def list_custom_kpis(
    campaign_id: Annotated[UUID | None, Query()] = None,
    include_deleted: Annotated[bool, Query()] = False,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CustomKpiListResponse:
    stmt = select(CustomKpi).where(CustomKpi.tenant_id == user.tenant_id)
    if campaign_id is not None:
        # Tenant-wide KPIs (campaign_id IS NULL) are always visible too.
        stmt = stmt.where(
            or_(CustomKpi.campaign_id == campaign_id, CustomKpi.campaign_id.is_(None))
        )
    if not include_deleted:
        stmt = stmt.where(CustomKpi.deleted_at.is_(None))
    stmt = stmt.order_by(CustomKpi.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return CustomKpiListResponse(
        items=[CustomKpiOut.model_validate(r) for r in rows], total=len(rows)
    )


@router.delete(
    "/custom-kpis/{kpi_id}",
    response_model=CustomKpiOut,
)
async def delete_custom_kpi(
    kpi_id: UUID,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CustomKpiOut:
    kpi = await db.get(CustomKpi, kpi_id)
    if kpi is None or kpi.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="kpi not found")
    if kpi.deleted_at is None:
        kpi.deleted_at = datetime.now(UTC)
        await db.flush()
    return CustomKpiOut.model_validate(kpi)


@router.get(
    "/campaigns/{campaign_id}/custom-kpis",
    response_model=CustomKpiEvaluationListResponse,
)
async def list_evaluated_custom_kpis(
    campaign_id: UUID,
    include_deleted: Annotated[bool, Query()] = False,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CustomKpiEvaluationListResponse:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")

    stmt = select(CustomKpi).where(
        CustomKpi.tenant_id == user.tenant_id,
        or_(CustomKpi.campaign_id == campaign_id, CustomKpi.campaign_id.is_(None)),
    )
    if not include_deleted:
        stmt = stmt.where(CustomKpi.deleted_at.is_(None))
    kpis = (await db.execute(stmt)).scalars().all()

    now = datetime.now(UTC)
    items: list[CustomKpiEvaluation] = []
    for kpi in kpis:
        result = await evaluate_custom_kpi(
            db, kpi=kpi, campaign_id=campaign_id, now=now
        )
        items.append(
            CustomKpiEvaluation(
                kpi_id=kpi.id,
                name=kpi.name,
                value=result.value,
                missing_event=result.missing_event,
                message=result.message,
            )
        )
    return CustomKpiEvaluationListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# Spend reconciliation
# ---------------------------------------------------------------------------


@router.post(
    "/spend-reconciliation/run",
    response_model=SpendReconciliationListResponse,
)
async def run_spend_reconciliation(
    body: RunReconciliationRequest,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> SpendReconciliationListResponse:
    rows = await run_reconciliation(
        db,
        tenant_id=user.tenant_id,
        period_start=body.period_start,
        period_end=body.period_end,
        invoices=body.invoices,
    )
    write_audit(
        db,
        tenant_id=user.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="spend_reconciliation",
        entity_id=user.id,  # batch op — anchor to the actor
        action="reconciliation_run",
        before_state=None,
        after_state=None,
        metadata={
            "period_start": body.period_start.isoformat(),
            "period_end": body.period_end.isoformat(),
            "campaigns": len(body.invoices),
        },
    )
    return SpendReconciliationListResponse(
        items=[SpendReconciliationOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get(
    "/campaigns/{campaign_id}/reconciliation",
    response_model=SpendReconciliationListResponse,
)
async def list_campaign_reconciliation(
    campaign_id: UUID,
    user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> SpendReconciliationListResponse:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="campaign not found")
    rows = (
        await db.execute(
            select(SpendReconciliation)
            .where(SpendReconciliation.campaign_id == campaign_id)
            .order_by(SpendReconciliation.period_end.desc())
        )
    ).scalars().all()
    return SpendReconciliationListResponse(
        items=[SpendReconciliationOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.post(
    "/spend-reconciliation/{recon_id}/explain",
    response_model=SpendReconciliationOut,
)
async def explain_reconciliation(
    recon_id: UUID,
    body: ExplainReconciliationRequest,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> SpendReconciliationOut:
    row = await db.get(SpendReconciliation, recon_id)
    if row is None or row.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="reconciliation not found")
    updated = await mark_explained(
        db,
        reconciliation_id=recon_id,
        user_id=user.id,
        note=body.note,
        now=datetime.now(UTC),
    )
    return SpendReconciliationOut.model_validate(updated)


@router.post(
    "/spend-reconciliation/{recon_id}/dispute",
    response_model=SpendReconciliationOut,
)
async def dispute_reconciliation(
    recon_id: UUID,
    body: DisputeReconciliationRequest,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> SpendReconciliationOut:
    row = await db.get(SpendReconciliation, recon_id)
    if row is None or row.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="reconciliation not found")
    updated = await mark_disputed(
        db,
        reconciliation_id=recon_id,
        user_id=user.id,
        note=body.note,
        now=datetime.now(UTC),
    )
    return SpendReconciliationOut.model_validate(updated)
