"""LinkedIn webhook handler (W33, stub).

Registered so the dispatcher framework accommodates LinkedIn-shaped
URLs and the raw payload still lands in `raw_webhook` for audit. Real
event mapping requires research on LinkedIn's signing scheme +
event-payload shapes that's deferred to a polish unit.

For W33:
  * Signature check uses the same shared-secret pattern as SendGrid so
    the dispatcher pathway is exercisable end-to-end. Real LinkedIn
    uses HMAC-SHA256 with their dev-portal-issued secret; the swap is
    a one-method change when we get to it.
  * `map_to_events` always returns [] → every LinkedIn webhook lands
    as 'unmapped' in the admin view, surfacing the payloads we'd need
    to schema-out for the real handler.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

from app.webhooks.base import (
    MappedEvent,
    ProviderHandler,
    SignatureCheck,
)


class LinkedInWebhookHandler(ProviderHandler):
    provider: ClassVar[str] = "linkedin"

    async def verify_signature(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        credential_payload: dict[str, Any] | None,
    ) -> SignatureCheck:
        if credential_payload is None:
            return SignatureCheck(valid=False, reason="no_credential")
        # LinkedIn's real scheme is HMAC-SHA256 of the body keyed on the
        # dev-portal client secret. Until that lands, fall back to the
        # same shared-secret pattern SendGrid uses so we can exercise
        # the dispatcher end-to-end against a stub.
        expected = credential_payload.get("webhook_secret")
        if not expected:
            return SignatureCheck(valid=False, reason="webhook_secret_not_configured")
        provided = headers.get("x-mas-webhook-secret") or headers.get(
            "X-MAS-Webhook-Secret"
        )
        if not provided or provided != expected:
            return SignatureCheck(valid=False, reason="secret_mismatch")
        return SignatureCheck(valid=True)

    async def map_to_events(
        self,
        *,
        body: bytes,
        tenant_id: UUID,
    ) -> list[MappedEvent]:
        # Intentional stub — the framework returns [] so the raw row
        # lands as 'unmapped' for the admin view.
        return []


__all__ = ["LinkedInWebhookHandler"]
