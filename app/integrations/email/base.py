"""Provider-agnostic email connector interface (W27, E12-S02).

The dispatch tool + API layer talk only to this abstract class. SendGrid is
the first implementation; SES / Postmark drop in alongside it later.

Three responsibilities per provider:

  * `send_batch(message, recipients)` — push a batch to the provider, return
    per-recipient acceptance + provider message IDs.
  * `send_test(from_email, to_email, subject, body)` — one-off send for the
    'send-test works' AC (E12-S02 #1). Same code path as a real dispatch but
    bypasses suppression/validation since it's an admin-initiated probe.
  * `parse_webhook(payload)` — turn the provider's event batch into the
    normalised `EmailWebhookEvent` shape the ingest endpoint writes to
    analytic_event + suppression_entry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


class EmailConnectorError(Exception):
    """Base for provider-tagged errors raised by the connector."""

    provider: str = ""

    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider or self.provider


class ProviderUnreachableError(EmailConnectorError):
    """Raised on transport-layer failures (connection refused, timeout, 5xx).

    Carries the provider name so the orchestrator can retry against the same
    provider rather than fail over to a different one (E11-S06 #2)."""


class ProviderRejectedError(EmailConnectorError):
    """The provider responded but rejected the request (4xx, validation
    failure, sender not verified server-side). Not retryable without
    changing the input."""


class UnknownProviderError(EmailConnectorError):
    """Raised by the factory when no connector matches the provider name."""


@dataclass(frozen=True)
class EmailRecipient:
    """One recipient in a dispatch batch.

    `merge_fields` are applied per-recipient when the connector substitutes
    placeholders in the message body (e.g. `{{first_name}}`)."""

    email: str
    merge_fields: dict[str, str] = field(default_factory=dict)
    name: str | None = None


@dataclass(frozen=True)
class EmailMessage:
    """The shared content of a dispatch batch. Per-recipient personalisation
    lives in `EmailRecipient.merge_fields`."""

    subject: str
    html_body: str | None = None
    text_body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    reply_to: str | None = None


@dataclass(frozen=True)
class SendResult:
    """Per-recipient outcome the connector returns from `send_batch`."""

    email: str
    provider_message_id: str | None
    accepted: bool
    error: str | None = None


@dataclass(frozen=True)
class EmailWebhookEvent:
    """Normalised webhook event the API layer writes to analytic_event +
    suppression_entry. One row per provider event after dedup on
    `provider_event_id`."""

    provider_event_id: str
    event_type: str  # one of: open, click, bounce, dropped, spam_complaint, unsubscribe, delivered, other
    email: str | None
    occurred_at: str | None  # ISO-8601; None if provider didn't supply
    payload: dict[str, Any] = field(default_factory=dict)


class EmailConnector(ABC):
    """One concrete subclass per email provider. Provider name MUST match
    `integration_credential.provider` for `build_email_connector` to find it."""

    provider: ClassVar[str]

    def __init__(self, *, payload: dict[str, Any]) -> None:
        self._payload = dict(payload)

    @property
    def api_key(self) -> str:
        return str(self._payload.get("api_key", ""))

    @property
    def verified_senders(self) -> list[str]:
        raw = self._payload.get("verified_senders") or []
        return [str(s).lower() for s in raw if isinstance(s, str)]

    @property
    def default_from_email(self) -> str | None:
        v = self._payload.get("default_from_email")
        return str(v) if isinstance(v, str) and v else None

    @property
    def webhook_secret(self) -> str | None:
        v = self._payload.get("webhook_secret")
        return str(v) if isinstance(v, str) and v else None

    @abstractmethod
    async def send_batch(
        self,
        *,
        from_email: str,
        message: EmailMessage,
        recipients: list[EmailRecipient],
    ) -> list[SendResult]:
        """Push a batch to the provider. Returns one `SendResult` per
        recipient in the same order they were passed in.

        Provider transport failures raise `ProviderUnreachableError`;
        provider rejections of the whole batch raise `ProviderRejectedError`.
        Per-recipient rejections are surfaced in the `SendResult.accepted`
        flag, not as exceptions."""

    @abstractmethod
    async def send_test(
        self,
        *,
        from_email: str,
        to_email: str,
        subject: str,
        body: str,
    ) -> SendResult:
        """One-off send for the 'send-test works' AC. Goes through the same
        provider endpoint as `send_batch` but with no suppression/validation
        layer — that's the dispatch tool's job, not the connector's."""

    @abstractmethod
    def parse_webhook(self, payload: Any) -> list[EmailWebhookEvent]:
        """Convert the provider's raw webhook payload into normalised events.

        Returns an empty list if `payload` is not a list/dict the provider
        recognises — never raises on shape issues, since the ingest endpoint
        receives untrusted input."""
