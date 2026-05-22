"""Public unsubscribe endpoint (W29, E16-S04 #2).

Recipients click an unsubscribe link in an email footer; that link carries
a signed token containing `{tenant_id, channel_platform, identifier}`.
This endpoint verifies the token (signed with the tenant's
`unsubscribe_secret`, fallback to `preview_share_secret` for dev) and
writes a `suppression_entry` immediately — well under the 5-second AC.

The endpoint is idempotent: a second unsubscribe of the same address is a
no-op (returns `already_existed=true`) so reload-spam doesn't error out.
Rotating `unsubscribe_secret` invalidates every URL already in the wild —
the documented revoke mechanism.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.schemas.unsubscribe import UnsubscribeResponse
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import ChannelPlatform
from app.db.models import (
    IntegrationCredential,
    SuppressionEntry,
    TenantComplianceSettings,
)
from app.db.session import set_tenant_context
from app.settings.config import get_settings

router = APIRouter(prefix="/api/unsubscribe", tags=["unsubscribe"])

_SERIALIZER_SALT = "email-unsubscribe"


@router.post(
    "/{token}",
    response_model=UnsubscribeResponse,
)
async def consume_unsubscribe_token(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> UnsubscribeResponse:
    """No auth — the signed token is the credential. Untrusted input, so
    we never leak token internals on error: invalid/expired/tampered tokens
    all return 404 with the same message."""
    payload = await _verify_token(db, token)
    tenant_id = UUID(payload["tenant_id"])
    channel = payload["channel_platform"]
    identifier = payload["identifier"]

    try:
        channel_enum = ChannelPlatform(channel)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="unsubscribe link not found"
        ) from exc

    await set_tenant_context(db, tenant_id)

    insert_stmt = (
        pg_insert(SuppressionEntry)
        .values(
            tenant_id=tenant_id,
            channel_platform=channel_enum,
            identifier=identifier,
            reason="unsubscribe",
        )
        .on_conflict_do_nothing(
            index_elements=[
                SuppressionEntry.tenant_id,
                SuppressionEntry.channel_platform,
                SuppressionEntry.identifier,
            ]
        )
        .returning(SuppressionEntry.id, SuppressionEntry.suppressed_at)
    )
    result = (await db.execute(insert_stmt)).first()
    already_existed = result is None

    if already_existed:
        existing = (
            await db.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.tenant_id == tenant_id,
                    SuppressionEntry.channel_platform == channel_enum,
                    SuppressionEntry.identifier == identifier,
                )
            )
        ).scalar_one()
        suppressed_at = existing.suppressed_at
    else:
        suppressed_at = datetime.now(UTC)
        # Audit only fresh unsubscribes — reload-spam shouldn't bloat the log.
        write_audit(
            db,
            tenant_id=tenant_id,
            actor_kind=current_actor_kind.get(),
            actor_id=current_actor_id.get(),
            entity_kind="suppression_entry",
            entity_id=result[0],
            action="unsubscribed",
            before_state=None,
            after_state={
                "channel_platform": channel,
                "identifier": identifier,
                "reason": "unsubscribe",
            },
            metadata={"source": "public_token"},
        )

    await db.commit()

    return UnsubscribeResponse(
        tenant_id=tenant_id,
        channel_platform=channel,
        identifier=identifier,
        suppressed_at=suppressed_at,
        already_existed=already_existed,
    )


async def _verify_token(db: AsyncSession, token: str) -> dict:
    """Try each tenant's unsubscribe_secret in turn until one verifies.

    For MVP we iterate — at the volumes we expect this is cheap. A
    tenant-id hint in the token shape lets us short-circuit later, but
    leaking that to attackers is undesirable so we keep it opaque."""
    settings_rows = (
        await db.execute(
            select(TenantComplianceSettings).where(
                TenantComplianceSettings.unsubscribe_secret.isnot(None)
            )
        )
    ).scalars().all()

    candidate_secrets = [
        row.unsubscribe_secret for row in settings_rows if row.unsubscribe_secret
    ]
    # Fallback to the dev preview-share secret so unsubscribes work even
    # before a tenant configures a dedicated secret.
    fallback = get_settings().effective_preview_share_secret()
    if fallback and fallback not in candidate_secrets:
        candidate_secrets.append(fallback)

    for secret in candidate_secrets:
        try:
            serializer = URLSafeSerializer(secret_key=secret, salt=_SERIALIZER_SALT)
            payload = serializer.loads(token)
        except BadSignature:
            continue
        if not isinstance(payload, dict):
            continue
        if not {"tenant_id", "channel_platform", "identifier"} <= payload.keys():
            continue
        return payload

    raise HTTPException(
        status.HTTP_404_NOT_FOUND, detail="unsubscribe link not found"
    )
