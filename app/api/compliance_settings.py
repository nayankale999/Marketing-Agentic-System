"""Tenant compliance settings endpoints (W29, E16-S04).

  - GET /api/compliance-settings  — current values (defaults when no row)
  - PUT /api/compliance-settings  — upsert postal_address + unsubscribe_secret

The unsubscribe_secret is write-only — `has_unsubscribe_secret` returns a
boolean indicator on read. Rotating the secret is the documented mechanism
for invalidating every unsubscribe URL already in the wild.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.compliance_settings import (
    ComplianceSettingsOut,
    ComplianceSettingsUpsert,
)
from app.db.enums import UserRole
from app.db.models import AppUser, TenantComplianceSettings

router = APIRouter(prefix="/api/compliance-settings", tags=["compliance"])


@router.get("", response_model=ComplianceSettingsOut)
async def get_compliance_settings(
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ComplianceSettingsOut:
    row = (
        await db.execute(
            select(TenantComplianceSettings).where(
                TenantComplianceSettings.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return ComplianceSettingsOut(
            id=row.id,
            tenant_id=row.tenant_id,
            postal_address=row.postal_address,
            has_unsubscribe_secret=bool(row.unsubscribe_secret),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
    return ComplianceSettingsOut(
        id=None,
        tenant_id=user.tenant_id,
        postal_address=None,
        has_unsubscribe_secret=False,
        created_at=None,
        updated_at=None,
    )


@router.put("", response_model=ComplianceSettingsOut)
async def upsert_compliance_settings(
    body: ComplianceSettingsUpsert,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ComplianceSettingsOut:
    row = (
        await db.execute(
            select(TenantComplianceSettings).where(
                TenantComplianceSettings.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantComplianceSettings(
            tenant_id=user.tenant_id,
            postal_address=body.postal_address,
            unsubscribe_secret=body.unsubscribe_secret,
        )
        db.add(row)
    else:
        row.postal_address = body.postal_address
        # Only overwrite the secret when the caller actually provided one —
        # PUT with `unsubscribe_secret=null` is treated as "clear it" only
        # when the caller explicitly passed null (which Pydantic distinguishes
        # from "omitted" via default=None + extra=forbid).
        if body.unsubscribe_secret is not None:
            row.unsubscribe_secret = body.unsubscribe_secret
        row.updated_at = datetime.now(UTC)
    await db.flush()
    return ComplianceSettingsOut(
        id=row.id,
        tenant_id=row.tenant_id,
        postal_address=row.postal_address,
        has_unsubscribe_secret=bool(row.unsubscribe_secret),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
