"""Inbound webhook endpoint (W33, E12-S06).

Single uniform receiver at `/webhooks/{provider}/{tenant_id}`. The
dispatcher does the heavy lifting; this layer is the HTTP shim.

Public — no OIDC session. The signed-secret-or-equivalent check is the
credential. Allow-listed in test_route_coverage."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.schemas.webhooks import WebhookIngestResult
from app.webhooks import dispatch_webhook
from app.webhooks.base import UnknownProviderError

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/{provider}/{tenant_id}",
    response_model=WebhookIngestResult,
)
async def receive_webhook(
    provider: str,
    tenant_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> WebhookIngestResult:
    """Provider-agnostic webhook receiver. Bad signature → 401 (after
    storing the raw row + audit trail); unknown provider → 404; valid →
    200 with summary (mapped events written + deduped count)."""
    try:
        # The dispatcher does NOT raise on UnknownProviderError — let the
        # registry raise here so we surface a clean 404 to the provider
        # without writing a raw row for a path that's misconfigured.
        from app.webhooks.registry import default_registry

        default_registry.get(provider)
    except UnknownProviderError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    result = await dispatch_webhook(
        db,
        provider=provider,
        tenant_id=tenant_id,
        headers=headers,
        body=body,
    )

    if not result.signature_valid:
        # Returning 401 honours AC #2. The raw + audit rows are already
        # committed by the dispatcher, so the rejection is durable.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={
                "reason": result.signature_reason or "invalid_signature",
                "raw_webhook_id": str(result.raw_webhook_id),
            },
        )

    return WebhookIngestResult(
        raw_webhook_id=result.raw_webhook_id,
        provider=provider,
        signature_valid=True,
        signature_reason=None,
        events_written=result.events_written,
        events_deduped=result.events_deduped,
    )
