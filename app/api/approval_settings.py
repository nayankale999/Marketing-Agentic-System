"""Tenant approval settings endpoints (W26, E07-S03/04).

  - GET /api/approval-settings — current values (or defaults if no row)
  - PUT /api/approval-settings — upsert (admin-only)

Single-row-per-tenant pattern. Missing row = defaults applied throughout the
codebase (admin_required_above=None, auto_approval_cap=0, currency=USD).
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.approval_settings import (
    ApprovalSettingsOut,
    ApprovalSettingsUpsert,
)
from app.db.enums import UserRole
from app.db.models import AppUser, TenantApprovalSettings

router = APIRouter(prefix="/api/approval-settings", tags=["approvals"])


@router.get("", response_model=ApprovalSettingsOut)
async def get_approval_settings(
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ApprovalSettingsOut:
    row = (
        await db.execute(
            select(TenantApprovalSettings).where(
                TenantApprovalSettings.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return ApprovalSettingsOut.model_validate(row)
    # No row yet — return the defaults the rest of the codebase falls back on.
    # Two decimal places match the NUMERIC(14,2) column shape so serialization
    # is consistent whether or not a row exists.
    return ApprovalSettingsOut(
        id=None,
        tenant_id=user.tenant_id,
        admin_required_above_amount=None,
        auto_approval_cap_amount=Decimal("0.00"),
        currency="USD",
        created_at=None,
        updated_at=None,
    )


@router.put("", response_model=ApprovalSettingsOut)
async def upsert_approval_settings(
    body: ApprovalSettingsUpsert,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> TenantApprovalSettings:
    row = (
        await db.execute(
            select(TenantApprovalSettings).where(
                TenantApprovalSettings.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = TenantApprovalSettings(
            tenant_id=user.tenant_id,
            admin_required_above_amount=body.admin_required_above_amount,
            auto_approval_cap_amount=body.auto_approval_cap_amount,
            currency=body.currency,
        )
        db.add(row)
    else:
        row.admin_required_above_amount = body.admin_required_above_amount
        row.auto_approval_cap_amount = body.auto_approval_cap_amount
        row.currency = body.currency
        row.updated_at = datetime.now(UTC)

    await db.flush()
    return row
