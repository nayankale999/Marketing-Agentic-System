"""ProviderHandler ABC + result types (W33, E12-S06).

Every provider's webhook integration boils down to two operations:

  * `verify_signature(headers, body, credential)` — provider-specific
    signing scheme check. Returns a `SignatureCheck` capturing valid +
    reason so failure rows can record why.

  * `map_to_events(payload, tenant_id)` — turn the provider's payload
    into zero-or-more `MappedEvent`s. The dispatcher inserts these as
    `analytic_event` rows with idempotency via `provider_event_id`.

Both methods take whatever shape the provider needs; the dispatcher
treats them as black boxes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID


class UnknownProviderError(Exception):
    """Raised by the registry when no handler matches a provider name."""


@dataclass(frozen=True)
class SignatureCheck:
    """Result of `ProviderHandler.verify_signature`. Capturing the reason
    lets the dispatcher persist *why* a row was rejected, not just that
    it was — useful for debugging misconfigured webhook secrets."""

    valid: bool
    reason: str | None = None


@dataclass(frozen=True)
class MappedEvent:
    """One `analytic_event`-shaped record the dispatcher will persist.

    `provider_event_id` is the dedup key. The dispatcher inserts via
    `ON CONFLICT DO NOTHING` against the existing partial unique index,
    so a replay of the same provider event is a no-op."""

    provider_event_id: str
    event_type: str  # one of EventKind values
    occurred_at: str | None = None  # ISO-8601; None falls back to now()
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchResult:
    """Summary the dispatcher returns for one inbound webhook."""

    signature_valid: bool
    signature_reason: str | None
    raw_webhook_id: UUID
    events_written: int
    events_deduped: int
    mapped_event_id: UUID | None  # First mapped event id, for raw_webhook back-link


class ProviderHandler(ABC):
    """One subclass per provider. The `provider` class attribute must
    match the URL path segment and `IntegrationCredential.provider`."""

    provider: ClassVar[str]

    @abstractmethod
    async def verify_signature(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        credential_payload: dict[str, Any] | None,
    ) -> SignatureCheck:
        """Per-provider signature scheme. `credential_payload` is the
        decrypted JSON from `IntegrationCredential.encrypted_payload`,
        or None when no credential is on file (the dispatcher still calls
        the handler so it can reject with 'no credential' reason)."""

    @abstractmethod
    async def map_to_events(
        self,
        *,
        body: bytes,
        tenant_id: UUID,
    ) -> list[MappedEvent]:
        """Turn the raw body into zero-or-more `MappedEvent`s. Returning
        an empty list flags the row as 'unmapped' — it lands in the
        admin debug view."""
