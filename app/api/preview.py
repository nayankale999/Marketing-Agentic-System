"""Asset preview + share-link endpoints (W24, E06-S07).

Four endpoints:

  - GET   /api/content-assets/{id}/preview                — resolve merge fields
  - POST  /api/content-assets/{id}/preview/audit-audience — per-field unresolved counts
  - POST  /api/content-assets/{id}/preview/share          — issue a signed token
  - GET   /api/preview-links/{token}                      — public token consumer

The public token endpoint is the only route in the app that intentionally
bypasses `require_role` — the token IS the credential. It's allow-listed
in tests/test_route_coverage.py alongside /health and /api/auth/*.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._preview import (
    audit_audience_resolution,
    channel_constraints_for,
    resolve_merge_fields,
)
from app.api.deps import get_db, get_tenant_db, require_role
from app.api.schemas.preview import (
    AudienceAuditEntry,
    AudienceAuditResponse,
    PreviewRequest,
    PreviewResponse,
    ShareRequest,
    ShareResponse,
)
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import UserRole
from app.db.models import AppUser, Audience, AudienceMember, ContentAsset
from app.db.session import set_tenant_context
from app.settings.config import get_settings

content_router = APIRouter(prefix="/api/content-assets", tags=["preview"])
public_router = APIRouter(prefix="/api/preview-links", tags=["preview"])


_SERIALIZER_SALT = "asset-preview-share"


def _serializer() -> URLSafeTimedSerializer:
    """Build the timed serializer fresh per request so settings changes
    (esp. in tests that monkeypatch the secret) take effect immediately."""
    secret = get_settings().effective_preview_share_secret()
    return URLSafeTimedSerializer(secret_key=secret, salt=_SERIALIZER_SALT)


def _asset_text_fields(asset: ContentAsset) -> dict[str, str]:
    """The fields the resolver scans for merge placeholders: the body plus
    any string-valued entries the CopywritingTool persisted under
    `metadata.fields` (subject, preheader, headline, cta, etc.)."""
    out: dict[str, str] = {}
    if asset.content:
        out["body"] = asset.content
    fields = asset.extra_metadata.get("fields") if asset.extra_metadata else None
    if isinstance(fields, dict):
        for k, v in fields.items():
            if isinstance(v, str):
                out[k] = v
    return out


def _channel_kind(asset: ContentAsset) -> str | None:
    if asset.extra_metadata:
        platform = asset.extra_metadata.get("channel_platform")
        if isinstance(platform, str) and platform:
            return platform
    return None


def _build_preview(
    asset: ContentAsset, *, sample_values: dict[str, str]
) -> PreviewResponse:
    rendered, report = resolve_merge_fields(
        _asset_text_fields(asset), sample_values=sample_values
    )
    channel_kind = _channel_kind(asset)
    constraints = channel_constraints_for(
        asset.asset_type.value, channel_kind
    )
    return PreviewResponse(
        asset_id=asset.id,
        asset_type=asset.asset_type.value,
        channel_kind=channel_kind,
        title=asset.title,
        rendered=rendered,
        referenced_fields=report.referenced_fields,
        unresolved_fields=report.unresolved_fields,
        resolved_with=report.resolved_fields,
        channel_constraints=constraints,
    )


@content_router.get("/{asset_id}/preview", response_model=PreviewResponse)
async def preview_asset(
    asset_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> PreviewResponse:
    """E06-S07 #1/#2: render the asset with default sample values. To swap
    values, POST to this same path — query-string sample values would land
    in the URL and bypass the route handler's body parsing."""
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")
    return _build_preview(asset, sample_values={})


@content_router.post("/{asset_id}/preview", response_model=PreviewResponse)
async def preview_asset_with_sample_values(
    asset_id: UUID,
    body: PreviewRequest,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> PreviewResponse:
    """E06-S07 #2: 'swap sample values, preview updates' — same endpoint as
    GET but takes a `sample_values` payload. Stateless: caller can re-call
    with different values cheaply."""
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")
    return _build_preview(asset, sample_values=body.sample_values)


@content_router.post(
    "/{asset_id}/preview/audit-audience",
    response_model=AudienceAuditResponse,
)
async def audit_against_audience(
    asset_id: UUID,
    _user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> AudienceAuditResponse:
    """E06-S07 #3: walk the campaign's most-recent audience and count members
    whose payload is missing each merge field. Returns per-field totals so
    a marketer can see 'first_name missing for 47 of 1000' before launch."""
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")

    audience = (
        await db.execute(
            select(Audience)
            .where(Audience.campaign_id == asset.campaign_id)
            .order_by(Audience.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if audience is None:
        return AudienceAuditResponse(
            asset_id=asset.id, total_members=0, field_audit=[]
        )

    payloads_rows = (
        await db.execute(
            select(AudienceMember.payload).where(
                AudienceMember.audience_id == audience.id
            )
        )
    ).all()
    payloads = [row[0] or {} for row in payloads_rows]
    total = len(payloads)

    _, report = resolve_merge_fields(_asset_text_fields(asset), sample_values={})
    counts = audit_audience_resolution(report.referenced_fields, payloads)
    entries = [
        AudienceAuditEntry(
            field=field,
            total_members=total,
            unresolved=counts[field]["unresolved"] if field in counts else total,
        )
        for field in report.referenced_fields
    ]
    return AudienceAuditResponse(
        asset_id=asset.id, total_members=total, field_audit=entries
    )


@content_router.post(
    "/{asset_id}/preview/share",
    response_model=ShareResponse,
    status_code=status.HTTP_201_CREATED,
)
async def share_preview(
    asset_id: UUID,
    body: ShareRequest,
    user: AppUser = Depends(require_role(UserRole.marketer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> ShareResponse:
    """E06-S07 #4: issue a signed time-bounded URL. itsdangerous embeds the
    payload in the token itself — no DB row needed for the token; the
    audit_log row is the durable record of the share."""
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="content asset not found")

    settings = get_settings()
    ttl_days = body.ttl_days or settings.preview_share_ttl_days
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)

    token = _serializer().dumps(
        {
            "asset_id": str(asset.id),
            "tenant_id": str(asset.tenant_id),
        }
    )

    write_audit(
        db,
        tenant_id=asset.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="content_asset",
        entity_id=asset.id,
        action="preview_shared",
        before_state=None,
        after_state=None,
        metadata={
            "shared_by_user_id": str(user.id),
            "ttl_days": ttl_days,
            "expires_at": expires_at.isoformat(),
        },
    )
    await db.flush()

    return ShareResponse(
        asset_id=asset.id,
        token=token,
        url_path=f"/api/preview-links/{token}",
        expires_at=expires_at,
    )


@public_router.get("/{token}", response_model=PreviewResponse)
async def consume_share_token(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> PreviewResponse:
    """Public — no role required. The signed token IS the credential.

    Token verification:
      - Bad signature       → 404 (don't leak existence of unsigned tokens)
      - Expired             → 410 Gone (token used to be valid)
      - Valid + asset gone  → 404
      - Valid + asset       → preview JSON

    The endpoint sets `app.tenant_id` from the token payload so the asset
    fetch respects RLS — a tampered tenant_id can't reach another tenant's
    rows even if the signature were somehow forged."""
    settings = get_settings()
    max_age = settings.preview_share_ttl_days * 86400

    try:
        payload = _serializer().loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="preview link has expired") from exc
    except BadSignature as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="preview link not found") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="preview link not found")

    asset_id_raw = payload.get("asset_id")
    tenant_id_raw = payload.get("tenant_id")
    try:
        asset_id = UUID(str(asset_id_raw))
        tenant_id = UUID(str(tenant_id_raw))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="preview link not found") from exc

    await set_tenant_context(db, tenant_id)
    asset = await db.get(ContentAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="preview link not found")
    return _build_preview(asset, sample_values={})
