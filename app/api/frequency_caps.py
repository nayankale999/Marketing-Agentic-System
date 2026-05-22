"""Frequency-cap configuration endpoints (W29, E08-S04 #2).

Admin-only — caps gate live sends, not just configuration. Per-tenant
per-channel: a tenant can run aggressive email caps while leaving LinkedIn
uncapped (or vice versa). Default is `enabled=false` so existing tenants
don't get behavior changes on upgrade.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.frequency_caps import (
    FrequencyCapListResponse,
    FrequencyCapOut,
    FrequencyCapUpsert,
)
from app.db.enums import ChannelPlatform, UserRole
from app.db.models import AppUser, FrequencyCapSetting

router = APIRouter(prefix="/api/frequency-caps", tags=["frequency-caps"])


@router.get("", response_model=FrequencyCapListResponse)
async def list_frequency_caps(
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> FrequencyCapListResponse:
    rows = (
        await db.execute(
            select(FrequencyCapSetting).order_by(
                FrequencyCapSetting.channel_platform.asc()
            )
        )
    ).scalars().all()
    return FrequencyCapListResponse(
        items=[FrequencyCapOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get("/{channel_platform}", response_model=FrequencyCapOut)
async def get_frequency_cap(
    channel_platform: ChannelPlatform,
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> FrequencyCapSetting:
    row = (
        await db.execute(
            select(FrequencyCapSetting).where(
                FrequencyCapSetting.channel_platform == channel_platform,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"no frequency cap configured for {channel_platform.value}",
        )
    return row


@router.put("/{channel_platform}", response_model=FrequencyCapOut)
async def upsert_frequency_cap(
    channel_platform: ChannelPlatform,
    body: FrequencyCapUpsert,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> FrequencyCapSetting:
    row = (
        await db.execute(
            select(FrequencyCapSetting).where(
                FrequencyCapSetting.tenant_id == user.tenant_id,
                FrequencyCapSetting.channel_platform == channel_platform,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = FrequencyCapSetting(
            tenant_id=user.tenant_id,
            channel_platform=channel_platform,
            max_sends_per_recipient=body.max_sends_per_recipient,
            window_days=body.window_days,
            enabled=body.enabled,
        )
        db.add(row)
    else:
        row.max_sends_per_recipient = body.max_sends_per_recipient
        row.window_days = body.window_days
        row.enabled = body.enabled
        row.updated_at = datetime.now(UTC)
    await db.flush()
    return row


@router.delete(
    "/{channel_platform}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_frequency_cap(
    channel_platform: ChannelPlatform,
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> None:
    row = (
        await db.execute(
            select(FrequencyCapSetting).where(
                FrequencyCapSetting.channel_platform == channel_platform,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"no frequency cap configured for {channel_platform.value}",
        )
    await db.delete(row)
    await db.flush()
