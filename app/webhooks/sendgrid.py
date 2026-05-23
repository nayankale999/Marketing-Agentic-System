"""SendGrid webhook handler (W33, wrapping W27 mapping logic).

W27 already shipped the SendGrid event parser via the existing
`SendGridConnector.parse_webhook` method; W33 wires it into the
uniform dispatcher.

Signature scheme: shared secret in `X-MAS-Webhook-Secret` header,
matched against the credential's `webhook_secret`. Production-grade
ECDSA signing is a polish unit (E12-S06's "signature verified per
provider" allows for provider-specific schemes; shared secret is one
valid scheme).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from uuid import UUID

from app.integrations.email.sendgrid import SendGridConnector
from app.webhooks.base import (
    MappedEvent,
    ProviderHandler,
    SignatureCheck,
)


# SendGrid event_type → EventKind (matches W27's mapping so old + new
# endpoints write identical analytic_event rows).
_EVENT_KIND_FOR_NORMALISED: dict[str, str] = {
    "delivered": "impression",
    "open": "open",
    "click": "click",
    "bounce": "bounce",
    "spam_complaint": "spam_complaint",
    "unsubscribe": "unsubscribe",
}


class SendGridWebhookHandler(ProviderHandler):
    provider: ClassVar[str] = "sendgrid"

    def __init__(self) -> None:
        # We construct a throwaway connector to reuse parse_webhook —
        # the connector's payload requirements are minimal for the parser
        # path (it just needs to instantiate). No API key needed.
        self._connector = SendGridConnector(payload={"api_key": "parser-only"})

    async def verify_signature(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        credential_payload: dict[str, Any] | None,
    ) -> SignatureCheck:
        if credential_payload is None:
            return SignatureCheck(valid=False, reason="no_credential")
        expected = credential_payload.get("webhook_secret")
        if not expected:
            return SignatureCheck(valid=False, reason="webhook_secret_not_configured")
        provided = headers.get("x-mas-webhook-secret") or headers.get(
            "X-MAS-Webhook-Secret"
        )
        if not provided:
            return SignatureCheck(valid=False, reason="missing_header")
        if provided != expected:
            return SignatureCheck(valid=False, reason="secret_mismatch")
        return SignatureCheck(valid=True)

    async def map_to_events(
        self,
        *,
        body: bytes,
        tenant_id: UUID,
    ) -> list[MappedEvent]:
        try:
            payload: Any = json.loads(body) if body else []
        except json.JSONDecodeError:
            return []

        out: list[MappedEvent] = []
        for evt in self._connector.parse_webhook(payload):
            event_kind = _EVENT_KIND_FOR_NORMALISED.get(evt.event_type)
            if event_kind is None:
                # 'other' / 'deferred' / 'processed' — don't write an
                # analytic_event row but keep the raw webhook trail.
                continue
            out.append(
                MappedEvent(
                    provider_event_id=evt.provider_event_id,
                    event_type=event_kind,
                    occurred_at=evt.occurred_at,
                    payload=evt.payload,
                )
            )
        return out


__all__ = ["SendGridWebhookHandler"]
