"""HTTP endpoints for external integrations.

W11 ships HubSpot (CRM) — three endpoints:
  - GET  /api/integrations/hubspot/connect   admin only; 302 to HubSpot OAuth
  - GET  /api/integrations/hubspot/callback  admin only; finalises OAuth + saves
  - POST /api/integrations/hubspot/test      admin only; lists a few contacts to prove the credential works

Salesforce + Dynamics get parallel routes once their connectors land.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.integration import (
    HubSpotTestResponse,
    IntegrationStatus,
    PlausibleSyncRequest,
    PlausibleSyncResponse,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import UserRole
from app.db.models import AppUser, IntegrationCredential
from app.integrations.credentials import get_encrypted_payload
from app.integrations.hubspot import HubSpotConnector
from app.integrations.web_analytics.ingest import ingest_events
from app.integrations.web_analytics.plausible import PlausibleConnector
from app.settings.config import get_settings

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# How long before expiry we proactively refresh the access token.
_REFRESH_LEEWAY = timedelta(minutes=2)


def _hubspot_connector() -> HubSpotConnector:
    s = get_settings()
    return HubSpotConnector(client_id=s.hubspot_client_id, client_secret=s.hubspot_client_secret)


async def _load_hubspot_credential(
    db: AsyncSession, tenant_id: Any
) -> IntegrationCredential | None:
    stmt = select(IntegrationCredential).where(
        IntegrationCredential.tenant_id == tenant_id,
        IntegrationCredential.provider == HubSpotConnector.provider,
        IntegrationCredential.channel_id.is_(None),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


@router.get(
    "/hubspot/status",
    response_model=IntegrationStatus,
)
async def hubspot_status(
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> IntegrationStatus:
    """Return whether HubSpot is connected for this tenant."""
    cred = await _load_hubspot_credential(db, user.tenant_id)
    if cred is None:
        return IntegrationStatus(
            provider="hubspot", label="default", connected=False, expires_at=None
        )
    payload = get_encrypted_payload().decrypt(cred.encrypted_payload)
    return IntegrationStatus(
        provider="hubspot",
        label=cred.label,
        connected=True,
        expires_at=cred.expires_at,
        scopes=list(payload.get("scopes", [])),
    )


@router.get("/hubspot/connect", include_in_schema=False)
async def hubspot_connect(
    request: Request,
    _user: AppUser = Depends(require_role(UserRole.admin)),
) -> RedirectResponse:
    """Begin the HubSpot OAuth dance. Generates a CSRF state, parks it in the
    session, and 302's the browser at HubSpot's authorize endpoint."""
    settings = get_settings()
    if not settings.hubspot_client_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HUBSPOT_CLIENT_ID is not configured for this deployment",
        )
    state = secrets.token_urlsafe(32)
    request.session["hubspot_oauth_state"] = state
    url = _hubspot_connector().authorize_url(
        state=state, redirect_uri=settings.hubspot_redirect_uri
    )
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/hubspot/callback", include_in_schema=False)
async def hubspot_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict[str, str]:
    """Handle HubSpot's redirect: validate state, swap code for tokens, store."""
    expected = request.session.pop("hubspot_oauth_state", None)
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="oauth state mismatch")

    settings = get_settings()
    connector = _hubspot_connector()
    tokens = await connector.exchange_code(code=code, redirect_uri=settings.hubspot_redirect_uri)

    enc = get_encrypted_payload()
    payload_dict = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "scopes": tokens.scopes,
    }
    encrypted = enc.encrypt(payload_dict)

    existing = await _load_hubspot_credential(db, user.tenant_id)
    if existing is None:
        cred = IntegrationCredential(
            tenant_id=user.tenant_id,
            channel_id=None,
            provider=HubSpotConnector.provider,
            label="default",
            encrypted_payload=encrypted,
            expires_at=tokens.expires_at,
        )
        db.add(cred)
        await db.flush()
        # `after_insert` listener writes the 'created' audit row.
    else:
        before = {
            "expires_at": existing.expires_at.isoformat() if existing.expires_at else None,
        }
        existing.encrypted_payload = encrypted
        existing.expires_at = tokens.expires_at
        await db.flush()
        write_audit(
            db,
            tenant_id=user.tenant_id,
            actor_kind=current_actor_kind.get(),
            actor_id=current_actor_id.get(),
            entity_kind="integration_credential",
            entity_id=existing.id,
            action="reconnected",
            before_state=before,
            after_state={"expires_at": tokens.expires_at.isoformat()},
            metadata={"provider": "hubspot"},
        )

    return {"status": "connected", "provider": "hubspot"}


@router.post(
    "/hubspot/test",
    response_model=HubSpotTestResponse,
)
async def hubspot_test(
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HubSpotTestResponse:
    """List the first few contacts so the operator can verify the credential."""
    cred = await _load_hubspot_credential(db, user.tenant_id)
    if cred is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no hubspot credential — run /api/integrations/hubspot/connect first",
        )

    enc = get_encrypted_payload()
    payload = enc.decrypt(cred.encrypted_payload)
    connector = _hubspot_connector()

    access_token = payload["access_token"]
    refresh_token = payload.get("refresh_token")
    needs_refresh = (
        cred.expires_at is not None and cred.expires_at <= datetime.now(UTC) + _REFRESH_LEEWAY
    )
    if needs_refresh:
        if not refresh_token:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="hubspot credential expired and no refresh_token available",
            )
        new_tokens = await connector.refresh(refresh_token=refresh_token)
        payload["access_token"] = new_tokens.access_token
        if new_tokens.refresh_token:
            payload["refresh_token"] = new_tokens.refresh_token
        cred.encrypted_payload = enc.encrypt(payload)
        cred.expires_at = new_tokens.expires_at
        access_token = new_tokens.access_token

    contacts, next_after = await connector.list_contacts(access_token=access_token, limit=5)
    return HubSpotTestResponse(
        count=len(contacts),
        sample=[{"id": c.external_id, "properties": dict(c.properties)} for c in contacts[:3]],
        next_after=next_after,
    )


@router.post("/plausible/sync", response_model=PlausibleSyncResponse)
async def plausible_sync(
    body: PlausibleSyncRequest | None = None,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> PlausibleSyncResponse:
    """E12-S05 / E01-S04 — fetch web analytics for the last N days, dedup +
    attribute via UTM, persist into analytic_event."""
    settings = get_settings()
    if not settings.plausible_api_key or not settings.plausible_site_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plausible is not configured (PLAUSIBLE_API_KEY + PLAUSIBLE_SITE_ID)",
        )

    days = body.days if body is not None else 7
    until = datetime.now(UTC)
    since = until - timedelta(days=days)

    connector = PlausibleConnector(
        api_key=settings.plausible_api_key, site_id=settings.plausible_site_id
    )
    events = await connector.fetch_events(since=since, until=until)
    summary = await ingest_events(db, tenant_id=user.tenant_id, events=events)

    return PlausibleSyncResponse(
        fetched=len(events),
        imported=summary.imported,
        duplicates=summary.duplicates,
        unattributed=summary.unattributed,
        window_start=since,
        window_end=until,
    )
