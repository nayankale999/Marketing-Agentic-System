"""Social integration endpoints (W30, E12-S03).

  - GET  /api/integrations/social/{provider}/status     — connected? + expiry
  - GET  /api/integrations/social/{provider}/connect    — start OAuth (302)
  - GET  /api/integrations/social/{provider}/callback   — finalise OAuth
  - GET  /api/integrations/social/{provider}/pages      — list authorised pages
  - POST /api/integrations/social/{provider}/channels   — create channel from a page
  - POST /api/integrations/social/channels/{id}/send-test — verify a configured channel

W30 ships LinkedIn; X and Meta drop in as additional connector subclasses.
"""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.schemas.integrations_social import (
    AuthorisedPageOut,
    AuthorisedPagesResponse,
    CreateSocialChannelRequest,
    CreateSocialChannelResponse,
    SocialIntegrationStatus,
    SocialSendTestRequest,
    SocialSendTestResponse,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import ChannelPlatform, UserRole
from app.db.models import AppUser, Channel, IntegrationCredential
from app.integrations.credentials import get_encrypted_payload
from app.integrations.social import (
    OAuthRevokedError,
    SocialPost,
    UnknownSocialProviderError,
    build_social_connector,
)
from app.integrations.social.base import (
    ProviderRejectedError,
    ProviderUnreachableError,
)
from app.settings.config import get_settings

router = APIRouter(prefix="/api/integrations/social", tags=["integrations-social"])

# Maps provider name → (client_id, client_secret, redirect_uri) settings keys.
_PROVIDER_PLATFORM = {"linkedin": ChannelPlatform.linkedin}


def _provider_settings(provider: str) -> tuple[str, str, str]:
    s = get_settings()
    if provider == "linkedin":
        return s.linkedin_client_id, s.linkedin_client_secret, s.linkedin_redirect_uri
    raise UnknownSocialProviderError(f"unknown social provider: {provider!r}")


def _build_connector(provider: str):
    client_id, client_secret, _ = _provider_settings(provider)
    return build_social_connector(
        provider, client_id=client_id, client_secret=client_secret
    )


async def _load_credential(
    db: AsyncSession, *, tenant_id: Any, provider: str
) -> IntegrationCredential | None:
    """Find the unattached (channel_id IS NULL) credential — the one created
    by the OAuth callback before the admin picks a page. After page
    selection we keep this row around and create a channel-attached copy
    for each selected page."""
    return (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.tenant_id == tenant_id,
                IntegrationCredential.provider == provider,
                IntegrationCredential.channel_id.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _load_channel_credential(
    db: AsyncSession, *, tenant_id: Any, channel_id: UUID
) -> tuple[Channel, IntegrationCredential]:
    channel = await db.get(Channel, channel_id)
    if channel is None or channel.tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="channel not found"
        )
    cred = (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.channel_id == channel_id,
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no credential attached to this channel",
        )
    return channel, cred


# ---------------------------------------------------------------------------
# Status + OAuth start
# ---------------------------------------------------------------------------


@router.get(
    "/{provider}/status", response_model=SocialIntegrationStatus
)
async def social_status(
    provider: str,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> SocialIntegrationStatus:
    cred = await _load_credential(db, tenant_id=user.tenant_id, provider=provider)
    if cred is None:
        return SocialIntegrationStatus(
            provider=provider, connected=False, expires_at=None
        )
    payload = get_encrypted_payload().decrypt(cred.encrypted_payload)
    return SocialIntegrationStatus(
        provider=provider,
        connected=True,
        expires_at=cred.expires_at,
        scopes=list(payload.get("scopes", [])),
    )


@router.get("/{provider}/connect", include_in_schema=False)
async def social_connect(
    provider: str,
    request: Request,
    _user: AppUser = Depends(require_role(UserRole.admin)),
) -> RedirectResponse:
    """Start OAuth: park a CSRF state in the session and 302 to the provider."""
    try:
        client_id, _client_secret, redirect_uri = _provider_settings(provider)
    except UnknownSocialProviderError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    if not client_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{provider.upper()}_CLIENT_ID is not configured for this deployment",
        )

    state = secrets.token_urlsafe(32)
    request.session[f"{provider}_oauth_state"] = state
    connector = _build_connector(provider)
    url = connector.authorize_url(state=state, redirect_uri=redirect_uri)
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/{provider}/callback", include_in_schema=False)
async def social_callback(
    provider: str,
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict[str, str]:
    """Handle the provider's redirect: validate state, swap code for tokens,
    save the unattached credential. The admin then calls `/pages` and
    `/channels` to pick which page to publish on behalf of."""
    expected = request.session.pop(f"{provider}_oauth_state", None)
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="oauth state mismatch"
        )

    try:
        _, _, redirect_uri = _provider_settings(provider)
    except UnknownSocialProviderError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    connector = _build_connector(provider)
    tokens = await connector.exchange_code(code=code, redirect_uri=redirect_uri)

    payload = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "scopes": tokens.scopes,
    }
    encrypted = get_encrypted_payload().encrypt(payload)

    existing = await _load_credential(db, tenant_id=user.tenant_id, provider=provider)
    if existing is None:
        cred = IntegrationCredential(
            tenant_id=user.tenant_id,
            channel_id=None,
            provider=provider,
            label="default",
            encrypted_payload=encrypted,
            expires_at=tokens.expires_at,
        )
        db.add(cred)
        await db.flush()
    else:
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
            before_state=None,
            after_state={"expires_at": tokens.expires_at.isoformat()},
            metadata={"provider": provider},
        )

    return {"status": "connected", "provider": provider}


# ---------------------------------------------------------------------------
# Pages + channel selection
# ---------------------------------------------------------------------------


@router.get(
    "/{provider}/pages", response_model=AuthorisedPagesResponse
)
async def list_pages(
    provider: str,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AuthorisedPagesResponse:
    cred = await _load_credential(db, tenant_id=user.tenant_id, provider=provider)
    if cred is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"{provider} is not connected; run /connect first",
        )
    payload = get_encrypted_payload().decrypt(cred.encrypted_payload)
    connector = _build_connector(provider)

    try:
        pages = await connector.list_authorised_pages(
            access_token=payload["access_token"]
        )
    except OAuthRevokedError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"{provider} authorisation revoked; reconnect required",
        ) from exc
    except (ProviderUnreachableError, ProviderRejectedError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return AuthorisedPagesResponse(
        provider=provider,
        items=[
            AuthorisedPageOut(page_id=p.page_id, page_name=p.page_name, urn=p.urn)
            for p in pages
        ],
    )


@router.post(
    "/{provider}/channels",
    response_model=CreateSocialChannelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_channel(
    provider: str,
    body: CreateSocialChannelRequest,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> CreateSocialChannelResponse:
    """Resolve a page from the OAuthed credential and persist a Channel
    row + a per-channel credential copy.

    We duplicate the credential (channel_id NULL → channel_id=X) so a
    tenant with multiple pages can publish to each independently without
    them sharing a row. The unattached one stays so re-OAuth works."""
    if provider not in _PROVIDER_PLATFORM:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"unknown social provider: {provider!r}"
        )
    platform = _PROVIDER_PLATFORM[provider]

    cred = await _load_credential(db, tenant_id=user.tenant_id, provider=provider)
    if cred is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"{provider} is not connected; run /connect first",
        )
    payload = get_encrypted_payload().decrypt(cred.encrypted_payload)
    connector = _build_connector(provider)

    try:
        pages = await connector.list_authorised_pages(
            access_token=payload["access_token"]
        )
    except OAuthRevokedError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"{provider} authorisation revoked; reconnect required",
        ) from exc

    selected = next((p for p in pages if p.page_id == body.page_id), None)
    if selected is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_id '{body.page_id}' is not in your authorised pages",
        )

    # Create the channel + attached credential copy.
    channel = Channel(
        tenant_id=user.tenant_id,
        name=body.name or selected.page_name,
        platform=platform,
        api_config={
            "provider": provider,
            "page_id": selected.page_id,
            "page_urn": selected.urn,
            "page_name": selected.page_name,
        },
        is_active=True,
    )
    db.add(channel)
    await db.flush()

    channel_cred = IntegrationCredential(
        tenant_id=user.tenant_id,
        channel_id=channel.id,
        provider=provider,
        label=selected.page_id,
        encrypted_payload=cred.encrypted_payload,
        expires_at=cred.expires_at,
    )
    db.add(channel_cred)
    await db.flush()

    return CreateSocialChannelResponse(
        channel_id=channel.id,
        provider=provider,
        page_id=selected.page_id,
        page_name=selected.page_name,
    )


# ---------------------------------------------------------------------------
# Send-test
# ---------------------------------------------------------------------------


@router.post(
    "/channels/{channel_id}/send-test",
    response_model=SocialSendTestResponse,
)
async def social_send_test(
    channel_id: UUID,
    body: SocialSendTestRequest,
    user: AppUser = Depends(require_role(UserRole.admin)),
    db: AsyncSession = Depends(get_tenant_db),
) -> SocialSendTestResponse:
    """Publish a single test post against a configured channel to verify
    that the OAuth credential + page URN work end-to-end."""
    channel, cred = await _load_channel_credential(
        db, tenant_id=user.tenant_id, channel_id=channel_id
    )
    payload = get_encrypted_payload().decrypt(cred.encrypted_payload)
    api_config = channel.api_config or {}
    provider = str(api_config.get("provider", ""))
    page_urn = str(api_config.get("page_urn", ""))
    if not provider or not page_urn:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="channel api_config is missing provider or page_urn",
        )
    connector = _build_connector(provider)

    try:
        result = await connector.publish_post(
            access_token=payload["access_token"],
            page_urn=page_urn,
            post=SocialPost(text=body.text),
        )
    except OAuthRevokedError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except (ProviderUnreachableError, ProviderRejectedError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return SocialSendTestResponse(
        provider=provider,
        provider_post_id=result.provider_post_id,
        url=result.url,
    )
