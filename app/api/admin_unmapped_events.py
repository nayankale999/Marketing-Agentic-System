"""Admin 'unmapped events' surface (W33, E12-S06 #3).

The dispatcher stores every inbound webhook in `raw_webhook` — including
ones whose payload didn't map to any `analytic_event`. This endpoint
surfaces the unmapped ones so an ops engineer can debug provider
integrations (e.g., 'why isn't the LinkedIn org-event being recorded?')
without grep-ing logs.

Admin-only because the raw payload + headers may contain provider
metadata that's sensitive (delivery addresses, sender IPs, etc.).
The full UI page is a polish unit; for W33 the JSON surface is the
ops-engineer-friendly debugging hook.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.webhooks import (
    UnmappedWebhookListResponse,
    UnmappedWebhookOut,
)
from app.db.enums import UserRole
from app.db.models import AppUser, RawWebhook

router = APIRouter(prefix="/api/webhooks", tags=["webhooks-admin"])


@router.get("/unmapped", response_model=UnmappedWebhookListResponse)
async def list_unmapped_webhooks(
    provider: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> UnmappedWebhookListResponse:
    base = select(RawWebhook).where(RawWebhook.mapped_event_id.is_(None))
    if provider is not None:
        base = base.where(RawWebhook.provider == provider)

    rows = (
        await db.execute(base.order_by(RawWebhook.received_at.desc()).limit(limit))
    ).scalars().all()

    total_stmt = select(sa_func.count()).select_from(
        RawWebhook.__table__
    ).where(RawWebhook.mapped_event_id.is_(None))
    if provider is not None:
        total_stmt = total_stmt.where(RawWebhook.provider == provider)
    total = (await db.execute(total_stmt)).scalar_one()

    return UnmappedWebhookListResponse(
        items=[UnmappedWebhookOut.model_validate(r) for r in rows],
        total=int(total),
    )
