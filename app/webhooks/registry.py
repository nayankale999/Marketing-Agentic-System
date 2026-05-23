"""Webhook handler registry (W33, E12-S06).

A small singleton the API layer consults to dispatch by provider name.
Handlers register themselves at import time via `default_registry`.
"""

from __future__ import annotations

from typing import Any

from app.webhooks.base import ProviderHandler, UnknownProviderError


class ProviderRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ProviderHandler] = {}

    def register(self, handler: ProviderHandler) -> None:
        if handler.provider in self._handlers:
            raise ValueError(
                f"webhook handler for '{handler.provider}' already registered"
            )
        self._handlers[handler.provider] = handler

    def get(self, provider: str) -> ProviderHandler:
        normalised = (provider or "").strip().lower()
        if normalised not in self._handlers:
            raise UnknownProviderError(f"no webhook handler for provider {provider!r}")
        return self._handlers[normalised]

    def names(self) -> list[str]:
        return sorted(self._handlers.keys())


# Module-level singleton other modules push their handlers into.
default_registry = ProviderRegistry()


def register_builtin_handlers() -> None:
    """Wire the built-in handlers into the singleton. Idempotent — safe
    to call multiple times (registry would raise on duplicate registration,
    but we guard here too)."""
    from app.webhooks.linkedin import LinkedInWebhookHandler
    from app.webhooks.sendgrid import SendGridWebhookHandler

    for handler in (SendGridWebhookHandler(), LinkedInWebhookHandler()):
        if handler.provider in default_registry._handlers:  # noqa: SLF001 — internal guard
            continue
        default_registry.register(handler)


# Register on import so callers don't have to remember.
register_builtin_handlers()


__all__ = [
    "ProviderRegistry",
    "default_registry",
    "register_builtin_handlers",
]
