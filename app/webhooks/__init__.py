"""Webhook framework (W33, E12-S06).

The pieces:

  * `ProviderHandler` ABC — every provider's verifier + event mapper.
  * `ProviderRegistry` — name → handler lookup the dispatcher walks.
  * `dispatch_webhook(...)` — the core entry: resolve handler, verify
    signature, store raw row, map to events, return a summary.

API surface for inbound webhooks lives in `app.api.webhooks`; this
package is intentionally HTTP-agnostic so the dispatcher logic is
testable without spinning up the request lifecycle.
"""

from app.webhooks.base import (
    DispatchResult,
    MappedEvent,
    ProviderHandler,
    SignatureCheck,
    UnknownProviderError,
)
from app.webhooks.dispatcher import dispatch_webhook
from app.webhooks.linkedin import LinkedInWebhookHandler
from app.webhooks.registry import ProviderRegistry, default_registry
from app.webhooks.sendgrid import SendGridWebhookHandler

__all__ = [
    "DispatchResult",
    "LinkedInWebhookHandler",
    "MappedEvent",
    "ProviderHandler",
    "ProviderRegistry",
    "SendGridWebhookHandler",
    "SignatureCheck",
    "UnknownProviderError",
    "default_registry",
    "dispatch_webhook",
]
