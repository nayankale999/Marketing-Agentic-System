"""Provider rate-limit configuration endpoints (W31, E08-S06).

Per-tenant per-provider caps the dispatchers will pace against. Schema
+ admin CRUD ship in W31; full token-bucket enforcement during dispatch
is a polish unit on the W31 foundation (documented in the work-unit
trade-offs).

Provider names are free-form strings to match `integration_credential.provider`
(sendgrid, linkedin, ...). A typo creates an unused row rather than 404 —
admins can list/delete to clean up.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.provider_rate_limits import (
    ProviderRateLimitListResponse,
    ProviderRateLimitOut,
    ProviderRateLimitUpsert,
)
from app.db.enums import UserRole
from app.db.models import AppUser, ProviderRateLimit

router = APIRouter(prefix="/api/provider-rate-limits", tags=["provider-rate-limits"])


@router.get("", response_model=ProviderRateLimitListResponse)
async def list_rate_limits(
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ProviderRateLimitListResponse:
    rows = (
        await db.execute(
            select(ProviderRateLimit).order_by(ProviderRateLimit.provider.asc())
        )
    ).scalars().all()
    return ProviderRateLimitListResponse(
        items=[ProviderRateLimitOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get("/{provider}", response_model=ProviderRateLimitOut)
async def get_rate_limit(
    provider: str,
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ProviderRateLimit:
    row = (
        await db.execute(
            select(ProviderRateLimit).where(
                ProviderRateLimit.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"no rate limit configured for {provider!r}",
        )
    return row


@router.put("/{provider}", response_model=ProviderRateLimitOut)
async def upsert_rate_limit(
    provider: str,
    body: ProviderRateLimitUpsert,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ProviderRateLimit:
    row = (
        await db.execute(
            select(ProviderRateLimit).where(
                ProviderRateLimit.tenant_id == user.tenant_id,
                ProviderRateLimit.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ProviderRateLimit(
            tenant_id=user.tenant_id,
            provider=provider,
            requests_per_minute=body.requests_per_minute,
            enabled=body.enabled,
        )
        db.add(row)
    else:
        row.requests_per_minute = body.requests_per_minute
        row.enabled = body.enabled
        row.updated_at = datetime.now(UTC)
    await db.flush()
    return row


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rate_limit(
    provider: str,
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> None:
    row = (
        await db.execute(
            select(ProviderRateLimit).where(
                ProviderRateLimit.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"no rate limit configured for {provider!r}",
        )
    await db.delete(row)
    await db.flush()
