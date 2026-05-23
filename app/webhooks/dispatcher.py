"""Webhook dispatcher core (W33, E12-S06).

HTTP-agnostic — the API layer constructs the dispatcher's inputs from a
FastAPI Request, calls `dispatch_webhook`, returns the result. Keeping
the dispatcher pure makes it directly testable.

Flow:
  1. Resolve the provider handler from the registry.
  2. Load the tenant's credential for this provider (may be None — the
     handler still runs and can reject with 'no_credential' reason).
  3. Call `handler.verify_signature(headers, body, credential_payload)`.
  4. Insert a `raw_webhook` row regardless of validity — failed
     signatures need an audit trail too.
  5. On invalid signature: write an `audit_log` row + return early.
  6. On valid signature: call `handler.map_to_events` → insert any
     resulting `analytic_event` rows via ON CONFLICT DO NOTHING for
     idempotency (dedup on `(tenant_id, provider_event_id)`).
  7. Back-link `raw_webhook.mapped_event_id` to the first inserted event
     so the admin view can navigate from raw → mapped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import EventKind
from app.db.models import AnalyticEvent, IntegrationCredential, RawWebhook
from app.db.session import set_tenant_context
from app.integrations.credentials import get_encrypted_payload
from app.webhooks.base import (
    DispatchResult,
    ProviderHandler,
    SignatureCheck,
)
from app.webhooks.registry import default_registry


async def dispatch_webhook(
    session: AsyncSession,
    *,
    provider: str,
    tenant_id: UUID,
    headers: dict[str, str],
    body: bytes,
) -> DispatchResult:
    handler = default_registry.get(provider)
    credential_payload = await _load_credential_payload(
        session, tenant_id=tenant_id, provider=handler.provider
    )

    sig: SignatureCheck = await handler.verify_signature(
        headers=headers,
        body=body,
        credential_payload=credential_payload,
    )

    # Set tenant context BEFORE any writes so RLS gates them.
    await set_tenant_context(session, tenant_id)

    raw = RawWebhook(
        tenant_id=tenant_id,
        provider=handler.provider,
        signature_valid=sig.valid,
        signature_reason=sig.reason,
        headers=_normalise_headers(headers),
        payload=body,
    )
    session.add(raw)
    await session.flush()

    if not sig.valid:
        # E12-S06 #2: failed signature → audit row with provider tag.
        write_audit(
            session,
            tenant_id=tenant_id,
            actor_kind=current_actor_kind.get(),
            actor_id=current_actor_id.get(),
            entity_kind="raw_webhook",
            entity_id=raw.id,
            action="webhook_signature_failed",
            before_state=None,
            after_state=None,
            metadata={
                "provider": handler.provider,
                "reason": sig.reason or "invalid",
            },
        )
        await session.commit()
        return DispatchResult(
            signature_valid=False,
            signature_reason=sig.reason,
            raw_webhook_id=raw.id,
            events_written=0,
            events_deduped=0,
            mapped_event_id=None,
        )

    mapped = await handler.map_to_events(body=body, tenant_id=tenant_id)

    events_written = 0
    events_deduped = 0
    first_mapped_id: UUID | None = None

    for evt in mapped:
        event_kind = _coerce_event_kind(evt.event_type)
        if event_kind is None:
            continue
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
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            events_deduped += 1
        else:
            events_written += 1
            if first_mapped_id is None:
                first_mapped_id = row[0]

    if first_mapped_id is not None:
        raw.mapped_event_id = first_mapped_id
    await session.commit()

    return DispatchResult(
        signature_valid=True,
        signature_reason=None,
        raw_webhook_id=raw.id,
        events_written=events_written,
        events_deduped=events_deduped,
        mapped_event_id=first_mapped_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_credential_payload(
    session: AsyncSession, *, tenant_id: UUID, provider: str
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.tenant_id == tenant_id,
                IntegrationCredential.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return get_encrypted_payload().decrypt(row.encrypted_payload)


def _normalise_headers(headers: dict[str, str]) -> dict[str, str]:
    # Drop sensitive shared-secret headers from the persisted snapshot so the
    # audit trail doesn't leak the verifier value. We still trust the live
    # `headers` arg for signature verification — this only affects what we
    # store.
    redacted = {"x-mas-webhook-secret", "authorization", "cookie"}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in redacted:
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out


def _coerce_event_kind(value: str) -> EventKind | None:
    try:
        return EventKind(value)
    except ValueError:
        return None


def _parse_iso_or_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)


__all__ = ["dispatch_webhook"]
