"""Email provider integrations (W27, E12-S02).

SendGrid is the first implementation; SES / Postmark drop in as additional
`EmailConnector` subclasses. The dispatch tool + API layer talk to the
abstract class only — `build_email_connector(provider, payload)` is the
single factory the handler/API code uses to get a working connector for
the tenant's configured provider."""

from typing import Any

from app.integrations.email.base import (
    EmailConnector,
    EmailConnectorError,
    EmailMessage,
    EmailRecipient,
    EmailWebhookEvent,
    ProviderRejectedError,
    ProviderUnreachableError,
    UnknownProviderError,
)
from app.integrations.email.sendgrid import SendGridConnector


def build_email_connector(provider: str, payload: dict[str, Any]) -> EmailConnector:
    """Resolve a provider name to a concrete connector wrapping the credential
    payload. Raises `UnknownProviderError` for an unsupported provider so the
    caller can surface a clean 400."""
    name = (provider or "").strip().lower()
    if name == SendGridConnector.provider:
        return SendGridConnector(payload=payload)
    raise UnknownProviderError(f"unknown email provider: {provider!r}")


__all__ = [
    "EmailConnector",
    "EmailConnectorError",
    "EmailMessage",
    "EmailRecipient",
    "EmailWebhookEvent",
    "ProviderRejectedError",
    "ProviderUnreachableError",
    "SendGridConnector",
    "UnknownProviderError",
    "build_email_connector",
]
