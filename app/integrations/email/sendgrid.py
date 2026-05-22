"""SendGrid connector — the first concrete email provider (W27, E12-S02).

SendGrid uses an API-key bearer token and a single endpoint
(`POST /v3/mail/send`). Webhook events arrive as a JSON array.

Implementation notes:
  * Per-recipient personalisation goes in `personalizations[]` so each
    recipient gets a dedicated provider message ID via the X-Message-Id
    header SendGrid returns. For dispatch batches up to ~1000 recipients,
    a single API call is sufficient; larger batches need chunking (out of
    scope for W27).
  * `parse_webhook` is the source of truth for SendGrid event-type →
    normalised `event_type` mapping. The API layer reads from that mapping
    when it decides whether to also write a `suppression_entry`.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import httpx

from app.integrations.email.base import (
    EmailConnector,
    EmailMessage,
    EmailRecipient,
    EmailWebhookEvent,
    ProviderRejectedError,
    ProviderUnreachableError,
    SendResult,
)

_SENDGRID_API = "https://api.sendgrid.com/v3/mail/send"
_REQUEST_TIMEOUT = 10.0
_MERGE_FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# SendGrid event-type → normalised event_type the W27 webhook layer maps to
# EventKind + suppression rows. Anything not in this map lands as 'other'.
_SENDGRID_EVENT_MAP: dict[str, str] = {
    "delivered": "delivered",
    "open": "open",
    "click": "click",
    "bounce": "bounce",
    "dropped": "bounce",
    "deferred": "other",
    "processed": "other",
    "spamreport": "spam_complaint",
    "unsubscribe": "unsubscribe",
    "group_unsubscribe": "unsubscribe",
    "group_resubscribe": "other",
}


class SendGridConnector(EmailConnector):
    provider: ClassVar[str] = "sendgrid"

    async def send_batch(
        self,
        *,
        from_email: str,
        message: EmailMessage,
        recipients: list[EmailRecipient],
    ) -> list[SendResult]:
        if not recipients:
            return []
        if not self.api_key:
            raise ProviderRejectedError(
                "sendgrid api_key is missing", provider=self.provider
            )

        # Per-recipient personalisations let SendGrid return one Message-Id
        # per recipient via the `X-Message-Id-N` headers (with N indexing
        # the personalizations array). We rely on that ordering below.
        personalizations: list[dict[str, Any]] = []
        for r in recipients:
            entry: dict[str, Any] = {"to": [{"email": r.email, "name": r.name} if r.name else {"email": r.email}]}
            if r.merge_fields:
                entry["substitutions"] = {
                    f"{{{{{k}}}}}": v for k, v in r.merge_fields.items()
                }
            personalizations.append(entry)

        content_blocks: list[dict[str, str]] = []
        if message.text_body:
            content_blocks.append({"type": "text/plain", "value": message.text_body})
        if message.html_body:
            content_blocks.append({"type": "text/html", "value": message.html_body})
        if not content_blocks:
            raise ProviderRejectedError(
                "message must have html_body or text_body", provider=self.provider
            )

        body: dict[str, Any] = {
            "personalizations": personalizations,
            "from": {"email": from_email},
            "subject": message.subject,
            "content": content_blocks,
        }
        if message.headers:
            body["headers"] = dict(message.headers)
        if message.reply_to:
            body["reply_to"] = {"email": message.reply_to}

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    _SENDGRID_API,
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"sendgrid transport failure: {exc}", provider=self.provider
            ) from exc

        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"sendgrid {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"sendgrid {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )

        # SendGrid returns a top-level X-Message-Id plus the same id with a
        # `-N` suffix per personalization for accepted batches. For W27 we
        # treat the whole batch as accepted on a 2xx — fine-grained per-row
        # rejections come back through the webhook (bounce/dropped events).
        base_message_id = resp.headers.get("X-Message-Id", "")
        return [
            SendResult(
                email=r.email,
                provider_message_id=(
                    f"{base_message_id}.{idx}" if base_message_id else None
                ),
                accepted=True,
            )
            for idx, r in enumerate(recipients)
        ]

    async def send_test(
        self,
        *,
        from_email: str,
        to_email: str,
        subject: str,
        body: str,
    ) -> SendResult:
        results = await self.send_batch(
            from_email=from_email,
            message=EmailMessage(subject=subject, text_body=body),
            recipients=[EmailRecipient(email=to_email)],
        )
        return results[0]

    def parse_webhook(self, payload: Any) -> list[EmailWebhookEvent]:
        """SendGrid posts an array of event objects. We tolerate either an
        array or a single dict so a misconfigured upstream doesn't break
        ingestion silently."""
        events: list[dict[str, Any]] = []
        if isinstance(payload, list):
            events = [e for e in payload if isinstance(e, dict)]
        elif isinstance(payload, dict):
            events = [payload]

        out: list[EmailWebhookEvent] = []
        for raw in events:
            sg_event = str(raw.get("event", "")).lower()
            normalised = _SENDGRID_EVENT_MAP.get(sg_event, "other")

            # `sg_event_id` is SendGrid's unique per-event id; we fall back
            # to `sg_message_id` for older event types that don't include it.
            event_id = raw.get("sg_event_id") or raw.get("sg_message_id")
            if not event_id:
                continue  # we can't dedup without an id — drop quietly

            timestamp = raw.get("timestamp")
            occurred_at: str | None = None
            if isinstance(timestamp, (int, float)):
                from datetime import datetime, timezone

                occurred_at = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc
                ).isoformat()

            email_value = raw.get("email")
            email_str = (
                str(email_value).lower()
                if isinstance(email_value, str) and email_value
                else None
            )

            out.append(
                EmailWebhookEvent(
                    provider_event_id=str(event_id),
                    event_type=normalised,
                    email=email_str,
                    occurred_at=occurred_at,
                    payload=raw,
                )
            )
        return out


__all__ = ["SendGridConnector"]
