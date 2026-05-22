"""`email.dispatch` tool (W27, E11-S06).

The cross-provider concerns the agent shouldn't re-implement per provider:

  * suppression-list filtering — recipients in `suppression_entry` are
    dropped pre-send and surfaced as `{reason: "suppressed"}` rejections
  * client-side sender + recipient validation — bogus addresses and
    unverified From values are rejected before reaching the provider
  * forbidden-header guard — caller can't override provider-managed
    headers like `X-Mailer` or `Return-Path`
  * normalized result shape — `{batch_id, provider, accepted_count,
    rejected_count, per_message_ids, rejections}` regardless of provider

Unlike W17/W18 this tool is NOT registered in the global tool registry —
it needs a per-tenant connector + DB session at call time. The W28
Distribution handler constructs it per-task with the resolved connector;
if/when a second session-aware tool lands we revisit the registry to
support factory tools.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ChannelPlatform
from app.db.models import SuppressionEntry
from app.integrations.email.base import (
    EmailConnector,
    EmailMessage,
    EmailRecipient,
    ProviderUnreachableError,
)
from app.tools.base import Tool

# Headers we never let callers override — they're either set by the
# provider or carry safety semantics (Return-Path for bounce routing,
# X-Mailer for spam-score reasons).
_FORBIDDEN_HEADERS: frozenset[str] = frozenset(
    {
        "return-path",
        "x-mailer",
        "received",
        "dkim-signature",
        "list-unsubscribe",
        "list-unsubscribe-post",
    }
)

# Loose RFC-5322ish recipient regex. The point isn't strict compliance
# (impossible) — it's catching obvious typos and `@example.com` mistakes
# before they reach the provider.
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$"
)

# Hard-coded bogus-recipient domains — admins setting these in production
# is almost always a misconfiguration.
_BLOCKED_RECIPIENT_DOMAINS: frozenset[str] = frozenset(
    {"example.com", "example.org", "example.net", "test.test"}
)


class DispatchValidationError(Exception):
    """Raised when the whole batch fails validation (e.g. unverified sender).

    Per-recipient validation failures don't raise — they surface as rejections
    in the result so the rest of the batch still ships."""


@dataclass(frozen=True)
class _ValidatedRecipient:
    email: str
    reason: str | None  # None = ok to send; else a `rejections[]` reason


class EmailDispatchTool(Tool):
    name: ClassVar[str] = "email.dispatch"
    description: ClassVar[str] = (
        "Dispatch an email batch via the tenant's configured provider with "
        "suppression-list filtering, validation, and a normalized result."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "from_email": {"type": "string"},
            "audience_batch": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "merge_fields": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "name": {"type": "string"},
                    },
                    "required": ["email"],
                },
                "minItems": 1,
            },
            "message": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "html_body": {"type": "string"},
                    "text_body": {"type": "string"},
                    "headers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "reply_to": {"type": "string"},
                },
                "required": ["subject"],
            },
            "idempotency_key": {"type": "string"},
        },
        "required": ["from_email", "audience_batch", "message"],
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "batch_id": {"type": "string"},
            "provider": {"type": "string"},
            "accepted_count": {"type": "integer"},
            "rejected_count": {"type": "integer"},
            "per_message_ids": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "provider_message_id": {"type": "string"},
                    },
                },
            },
            "rejections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
        "required": [
            "batch_id",
            "provider",
            "accepted_count",
            "rejected_count",
            "per_message_ids",
            "rejections",
        ],
    }

    def __init__(
        self,
        *,
        connector: EmailConnector,
        session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> None:
        self._connector = connector
        self._session = session
        self._tenant_id = tenant_id

    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        from_email = str(inputs.get("from_email", "")).strip().lower()
        if not from_email:
            raise DispatchValidationError("from_email is required")

        # Whole-batch sender verification (AC E12-S02 #3, E11-S06 #3).
        verified = {s.lower() for s in self._connector.verified_senders}
        if verified and from_email not in verified:
            raise DispatchValidationError(
                f"from_email '{from_email}' is not in verified_senders"
            )

        message = self._build_message(inputs.get("message") or {})

        raw_recipients = inputs.get("audience_batch") or []
        if not isinstance(raw_recipients, list) or not raw_recipients:
            raise DispatchValidationError("audience_batch must be a non-empty list")

        suppressed = await self._load_suppressed_emails(
            [str(r.get("email", "")) for r in raw_recipients if isinstance(r, dict)]
        )

        # Per-recipient validation. We keep the input order so per_message_ids
        # is stable for clients correlating against the same batch.
        validated: list[_ValidatedRecipient] = []
        send_payload: list[EmailRecipient] = []
        rejections: list[dict[str, str]] = []
        for r in raw_recipients:
            if not isinstance(r, dict):
                rejections.append({"email": str(r), "reason": "invalid_shape"})
                continue
            email = str(r.get("email", "")).strip().lower()
            if not email:
                rejections.append({"email": "", "reason": "missing_email"})
                continue
            reason = self._validate_recipient(email, suppressed=suppressed)
            if reason is not None:
                rejections.append({"email": email, "reason": reason})
                continue
            validated.append(_ValidatedRecipient(email=email, reason=None))
            send_payload.append(
                EmailRecipient(
                    email=email,
                    merge_fields={
                        str(k): str(v)
                        for k, v in (r.get("merge_fields") or {}).items()
                    },
                    name=r.get("name"),
                )
            )

        per_message_ids: list[dict[str, str]] = []
        if send_payload:
            results = await self._connector.send_batch(
                from_email=from_email,
                message=message,
                recipients=send_payload,
            )
            for res in results:
                if res.accepted:
                    if res.provider_message_id:
                        per_message_ids.append(
                            {
                                "email": res.email,
                                "provider_message_id": res.provider_message_id,
                            }
                        )
                else:
                    rejections.append(
                        {
                            "email": res.email,
                            "reason": res.error or "provider_rejected",
                        }
                    )

        accepted_count = len(per_message_ids)
        # Some accepted recipients may not have a provider_message_id yet
        # (e.g. the provider returned 202 without IDs). Treat any non-rejected
        # validated recipient as accepted for the count.
        if accepted_count == 0 and send_payload and not rejections:
            accepted_count = len(send_payload)

        return {
            "batch_id": str(uuid.uuid4()),
            "provider": self._connector.provider,
            "accepted_count": accepted_count,
            "rejected_count": len(rejections),
            "per_message_ids": per_message_ids,
            "rejections": rejections,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_message(payload: dict[str, Any]) -> EmailMessage:
        subject = str(payload.get("subject", "")).strip()
        if not subject:
            raise DispatchValidationError("message.subject is required")

        headers = payload.get("headers") or {}
        if not isinstance(headers, dict):
            raise DispatchValidationError("message.headers must be an object")
        bad_headers = [h for h in headers if h.lower() in _FORBIDDEN_HEADERS]
        if bad_headers:
            raise DispatchValidationError(
                f"forbidden headers: {sorted(bad_headers)}"
            )

        return EmailMessage(
            subject=subject,
            html_body=payload.get("html_body"),
            text_body=payload.get("text_body"),
            headers={str(k): str(v) for k, v in headers.items()},
            reply_to=payload.get("reply_to"),
        )

    async def _load_suppressed_emails(self, candidates: list[str]) -> set[str]:
        """One round-trip to suppression_entry for the whole batch."""
        normalised = {c.strip().lower() for c in candidates if c}
        if not normalised:
            return set()
        rows = (
            await self._session.execute(
                select(SuppressionEntry.identifier).where(
                    SuppressionEntry.tenant_id == self._tenant_id,
                    SuppressionEntry.channel_platform == ChannelPlatform.email,
                    SuppressionEntry.identifier.in_(normalised),
                )
            )
        ).scalars().all()
        return {str(r).lower() for r in rows}

    @staticmethod
    def _validate_recipient(email: str, *, suppressed: set[str]) -> str | None:
        if email in suppressed:
            return "suppressed"
        if not _EMAIL_RE.match(email):
            return "invalid_address"
        domain = email.rsplit("@", 1)[-1].lower()
        if domain in _BLOCKED_RECIPIENT_DOMAINS:
            return "blocked_domain"
        return None


__all__ = ["DispatchValidationError", "EmailDispatchTool"]
