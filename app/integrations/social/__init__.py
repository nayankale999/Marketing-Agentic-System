"""Social platform integrations (W30, E12-S03).

LinkedIn is the first implementation; X and Meta drop in as additional
`SocialConnector` subclasses later. The dispatch tool + API layer talk to
the abstract class only — `build_social_connector(provider, ...)` is the
factory that resolves provider name → concrete connector.
"""

from app.integrations.social.base import (
    AuthorisedPage,
    MediaRequiredError,
    OAuthRevokedError,
    PostResult,
    SocialConnector,
    SocialConnectorError,
    SocialPost,
    UnknownSocialProviderError,
)
from app.integrations.social.linkedin import LinkedInConnector


def build_social_connector(
    provider: str,
    *,
    client_id: str,
    client_secret: str,
) -> SocialConnector:
    """Resolve a provider name to a concrete social connector. Raises
    `UnknownSocialProviderError` for an unsupported provider so the caller
    can surface a clean 400."""
    name = (provider or "").strip().lower()
    if name == LinkedInConnector.provider:
        return LinkedInConnector(client_id=client_id, client_secret=client_secret)
    raise UnknownSocialProviderError(f"unknown social provider: {provider!r}")


__all__ = [
    "AuthorisedPage",
    "LinkedInConnector",
    "MediaRequiredError",
    "OAuthRevokedError",
    "PostResult",
    "SocialConnector",
    "SocialConnectorError",
    "SocialPost",
    "UnknownSocialProviderError",
    "build_social_connector",
]
