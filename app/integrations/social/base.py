"""Provider-agnostic social connector interface (W30, E12-S03 + E11-S05).

The dispatch tool + API layer talk only to this abstract class. LinkedIn
is the first implementation; X and Meta drop in alongside it later.

Four responsibilities per provider:

  * OAuth flow — `authorize_url`, `exchange_code`, `refresh_tokens`.
    The state-bag + redirect handling lives in the API layer; the
    connector just builds URLs and exchanges codes.

  * `list_authorised_pages(access_token)` — once OAuth completes, the
    admin needs to pick which page/account this credential publishes to.
    Returns one row per addressable target (LinkedIn org page, X account,
    Meta page).

  * `publish_post(access_token, page_id, post)` — single-post publish.
    Returns the provider's post id + a publicly-viewable URL.

  * Revoked-auth detection — providers return distinct error codes when
    a token has been revoked or has expired beyond refresh. Surfacing
    that as `OAuthRevokedError` lets the dispatch agent pause the
    campaign cleanly per AC E12-S03 #3.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar


class SocialConnectorError(Exception):
    """Base for provider-tagged errors raised by the connector."""

    provider: str = ""

    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider or self.provider


class OAuthRevokedError(SocialConnectorError):
    """Token has been revoked, expired beyond refresh, or the connected app
    has been disconnected on the provider side. Distribution treats this
    as a hard pause: campaigns using this channel can't publish until an
    admin re-OAuths (E12-S03 #3)."""


class MediaRequiredError(SocialConnectorError):
    """Caller flagged `media_required=True` on the post but didn't include
    a media URL. Surfaced as a precondition error before the platform call
    so the operator sees a clear failure reason (E11-S05 #4)."""


class ProviderUnreachableError(SocialConnectorError):
    """Transport failure or 5xx — the dispatcher's retry/backoff path
    should pick this up without flipping the asset off scheduled."""


class ProviderRejectedError(SocialConnectorError):
    """4xx other than 401 — input has to change before retry."""


class UnknownSocialProviderError(SocialConnectorError):
    """Raised by the factory when no connector matches the provider name."""


@dataclass(frozen=True)
class OAuthTokens:
    """Result of a successful OAuth exchange or refresh."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AuthorisedPage:
    """One addressable target the credential can publish to."""

    page_id: str
    page_name: str
    urn: str  # LinkedIn URN, Meta page id, X user id — provider-specific opaque token
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SocialPost:
    """The content + scheduling intent for one social publish."""

    text: str
    media_url: str | None = None
    media_required: bool = False
    visibility: str = "PUBLIC"  # provider-mapped; default = public


@dataclass(frozen=True)
class PostResult:
    """What the connector returns after a successful publish."""

    provider_post_id: str
    url: str


class SocialConnector(ABC):
    """One concrete subclass per social provider. `provider` must match
    `integration_credential.provider` for the factory to resolve it."""

    provider: ClassVar[str]
    default_scopes: ClassVar[tuple[str, ...]]

    @abstractmethod
    def authorize_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        scopes: list[str] | None = None,
    ) -> str: ...

    @abstractmethod
    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> OAuthTokens: ...

    @abstractmethod
    async def refresh_tokens(self, *, refresh_token: str) -> OAuthTokens:
        """Refresh access token. Raises OAuthRevokedError when the refresh
        is rejected (revoked/expired)."""

    @abstractmethod
    async def list_authorised_pages(
        self, *, access_token: str
    ) -> list[AuthorisedPage]:
        """Pages/accounts the credential can publish to. May raise
        OAuthRevokedError on 401."""

    @abstractmethod
    async def publish_post(
        self,
        *,
        access_token: str,
        page_urn: str,
        post: SocialPost,
    ) -> PostResult:
        """Single-post publish. Returns the platform's post id + URL.
        Raises OAuthRevokedError on 401, MediaRequiredError if a required
        media is missing, ProviderUnreachableError on transport failure."""
