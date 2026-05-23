"""Email provider integration endpoints (W27, E12-S02).

  - POST /api/integrations/email/config      — upsert tenant's email credential
  - GET  /api/integrations/email/config      — read current config (api_key redacted)
  - POST /api/integrations/email/send-test   — admin one-off send to verify config
  - POST /api/integrations/email/webhook/{tenant_id} — provider event receiver

The webhook receiver intentionally bypasses `require_role` because the
shared secret IS the credential. It's allow-listed in
tests/test_route_coverage alongside /api/preview-links/{token}.

Webhook auth = `X-MAS-Webhook-Secret` header matched against the credential
payload's `webhook_secret`. The tenant_id in the URL gates which credential
we look up. ECDSA signature verification (SendGrid's native scheme) is a
polish unit — shared secret is sufficient for MVP.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_tenant_db, require_role
from app.api.schemas.integrations_email import (
    EmailConfigOut,
    EmailConfigUpsert,
    SendTestRequest,
    SendTestResponse,
    WebhookIngestResponse,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import ChannelPlatform, EventKind, UserRole
from app.db.models import (
    AnalyticEvent,
    AppUser,
    IntegrationCredential,
    SuppressionEntry,
)
from app.db.session import set_tenant_context
from app.integrations.credentials import get_encrypted_payload
from app.integrations.email import (
    EmailConnectorError,
    ProviderUnreachableError,
    UnknownProviderError,
    build_email_connector,
)

router = APIRouter(prefix="/api/integrations/email", tags=["integrations-email"])


# ---------------------------------------------------------------------------
# Config (encrypted credential)
# ---------------------------------------------------------------------------


@router.post(
    "/config",
    response_model=EmailConfigOut,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_email_config(
    body: EmailConfigUpsert,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> EmailConfigOut:
    """Encrypts the payload with Fernet (same wrapper as HubSpot creds) and
    persists a single credential row per tenant per provider — the partial
    unique index from migration 0006 enforces that constraint at the DB."""
    if body.default_from_email.lower() not in {
        s.lower() for s in body.verified_senders
    }:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="default_from_email must appear in verified_senders",
        )

    # Validate the provider is one we have a connector for, before persisting.
    try:
        build_email_connector(body.provider, payload={"api_key": "probe"})
    except UnknownProviderError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    payload = {
        "api_key": body.api_key,
        "default_from_email": str(body.default_from_email).lower(),
        "verified_senders": [s.lower() for s in body.verified_senders],
        "webhook_secret": body.webhook_secret,
    }
    encrypted = get_encrypted_payload().encrypt(payload)

    existing = (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.tenant_id == user.tenant_id,
                IntegrationCredential.provider == body.provider,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        row = IntegrationCredential(
            tenant_id=user.tenant_id,
            channel_id=None,
            provider=body.provider,
            label="default",
            encrypted_payload=encrypted,
            key_version=1,
        )
        db.add(row)
    else:
        existing.encrypted_payload = encrypted
        existing.key_version = 1
        existing.updated_at = datetime.now(UTC)
        row = existing

    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="email credential conflict",
        ) from exc

    write_audit(
        db,
        tenant_id=row.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="integration_credential",
        entity_id=row.id,
        action="email_config_saved",
        before_state=None,
        after_state=None,
        metadata={"provider": body.provider},
    )

    return _config_response(row, payload)


@router.get("/config", response_model=EmailConfigOut)
async def get_email_config(
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> EmailConfigOut:
    row = (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.tenant_id == user.tenant_id,
                IntegrationCredential.provider == "sendgrid",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no email integration configured",
        )
    payload = get_encrypted_payload().decrypt(row.encrypted_payload)
    return _config_response(row, payload)


# ---------------------------------------------------------------------------
# Send-test
# ---------------------------------------------------------------------------


@router.post("/send-test", response_model=SendTestResponse)
async def send_test(
    body: SendTestRequest,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> SendTestResponse:
    """E12-S02 #1 'send test works against a verified sender'."""
    connector, payload = await _load_connector(db, tenant_id=user.tenant_id)

    from_email = str(body.from_email or payload.get("default_from_email") or "").lower()
    verified = {s.lower() for s in payload.get("verified_senders", [])}
    if not from_email or (verified and from_email not in verified):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from_email is not in verified_senders",
        )

    try:
        result = await connector.send_test(
            from_email=from_email,
            to_email=str(body.to_email),
            subject=body.subject,
            body=body.body,
        )
    except ProviderUnreachableError as exc:
        return SendTestResponse(
            accepted=False, provider=connector.provider, provider_message_id=None,
            error=f"provider_unreachable: {exc}",
        )
    except EmailConnectorError as exc:
        return SendTestResponse(
            accepted=False, provider=connector.provider, provider_message_id=None,
            error=str(exc),
        )

    return SendTestResponse(
        accepted=result.accepted,
        provider=connector.provider,
        provider_message_id=result.provider_message_id,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Webhook receiver — public (shared-secret auth)
# ---------------------------------------------------------------------------


@router.post(
    "/webhook/{tenant_id}",
    response_model=WebhookIngestResponse,
)
async def receive_webhook(
    tenant_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_mas_webhook_secret: str | None = Header(default=None, alias="X-MAS-Webhook-Secret"),
) -> WebhookIngestResponse:
    """Ingest provider event batches (W27).

    DEPRECATED in favor of the uniform receiver at
    `/webhooks/sendgrid/{tenant_id}` (W33, E12-S06). This endpoint stays
    live so tenants already configured against it keep working; new
    integrations should point at the W33 path. Both paths share the same
    SendGrid event-mapping logic via `SendGridConnector.parse_webhook`,
    so analytic_event rows are identical regardless of which receiver
    landed them.

    Auth = shared secret in `X-MAS-Webhook-Secret` header matched against
    the credential payload."""
    # We don't depend on get_tenant_db here because the credential lookup
    # has to happen BEFORE we know the tenant context is safe to set.
    cred = (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.tenant_id == tenant_id,
                IntegrationCredential.provider == "sendgrid",
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no email integration")

    payload = get_encrypted_payload().decrypt(cred.encrypted_payload)
    expected_secret = payload.get("webhook_secret")
    if not expected_secret or not x_mas_webhook_secret or x_mas_webhook_secret != expected_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid webhook secret")

    # Body could be a JSON array or object — let the connector decide.
    raw_body = await request.body()
    try:
        raw_payload: Any = json.loads(raw_body) if raw_body else []
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"invalid json: {exc}") from exc

    connector = build_email_connector(cred.provider, payload=payload)
    events = connector.parse_webhook(raw_payload)
    received = len(events)

    # Switch to the tenant's RLS context for the writes.
    await set_tenant_context(db, tenant_id)

    written = 0
    deduped = 0
    suppression_writes = 0

    for evt in events:
        event_kind = _event_kind_for(evt.event_type)
        if event_kind is None:
            continue  # `other` events we keep out of analytic_event for now

        stmt = (
            pg_insert(AnalyticEvent)
            .values(
                tenant_id=tenant_id,
                event_type=event_kind,
                payload=evt.payload,
                provider_event_id=evt.provider_event_id,
                event_at=_parse_iso_or_now(evt.occurred_at),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    AnalyticEvent.tenant_id,
                    AnalyticEvent.provider_event_id,
                ],
                index_where=AnalyticEvent.provider_event_id.isnot(None),
            )
            .returning(AnalyticEvent.id)
        )
        result = await db.execute(stmt)
        row = result.first()
        if row is None:
            deduped += 1
            continue
        written += 1
        analytic_event_id = row[0]

        # Suppression rows for terminal bouncing/complaint/unsub events.
        suppress_reason = _suppression_reason_for(evt.event_type)
        if suppress_reason and evt.email:
            sup_stmt = (
                pg_insert(SuppressionEntry)
                .values(
                    tenant_id=tenant_id,
                    channel_platform=ChannelPlatform.email,
                    identifier=evt.email,
                    reason=suppress_reason,
                    source_event_id=analytic_event_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        SuppressionEntry.tenant_id,
                        SuppressionEntry.channel_platform,
                        SuppressionEntry.identifier,
                    ]
                )
                .returning(SuppressionEntry.id)
            )
            sup_result = await db.execute(sup_stmt)
            if sup_result.first() is not None:
                suppression_writes += 1

    # get_db doesn't auto-commit (production) — we own this transaction.
    await db.commit()
    return WebhookIngestResponse(
        received=received,
        written=written,
        deduped=deduped,
        suppression_writes=suppression_writes,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_connector(
    db: AsyncSession, *, tenant_id: UUID
) -> tuple[Any, dict[str, Any]]:
    """Load + decrypt the tenant's credential and return (connector, payload)."""
    row = (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.tenant_id == tenant_id,
                IntegrationCredential.provider == "sendgrid",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no email integration configured",
        )
    payload = get_encrypted_payload().decrypt(row.encrypted_payload)
    connector = build_email_connector(row.provider, payload=payload)
    return connector, payload


def _config_response(
    row: IntegrationCredential, payload: dict[str, Any]
) -> EmailConfigOut:
    api_key = str(payload.get("api_key", ""))
    last4 = api_key[-4:] if len(api_key) >= 4 else api_key
    return EmailConfigOut(
        id=row.id,
        tenant_id=row.tenant_id,
        provider=row.provider,
        default_from_email=str(payload.get("default_from_email", "")),
        verified_senders=list(payload.get("verified_senders", [])),
        api_key_last4=last4,
        has_webhook_secret=bool(payload.get("webhook_secret")),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


_EVENT_KIND_MAP: dict[str, EventKind] = {
    "delivered": EventKind.impression,
    "open": EventKind.open,
    "click": EventKind.click,
    "bounce": EventKind.bounce,
    "spam_complaint": EventKind.spam_complaint,
    "unsubscribe": EventKind.unsubscribe,
}


def _event_kind_for(event_type: str) -> EventKind | None:
    return _EVENT_KIND_MAP.get(event_type)


_SUPPRESSION_REASON_MAP: dict[str, str] = {
    "bounce": "bounce",
    "spam_complaint": "complaint",
    "unsubscribe": "unsubscribe",
}


def _suppression_reason_for(event_type: str) -> str | None:
    return _SUPPRESSION_REASON_MAP.get(event_type)


def _parse_iso_or_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)
