"""Social platform integrations (W30, E12-S03; W40 adds X + Meta).

LinkedIn shipped first; W40 adds X (Twitter) and Meta (Facebook Pages).
The dispatch tool + API layer talk to the abstract class only —
`build_social_connector(provider, ...)` is the factory that resolves
provider name → concrete connector.
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
from app.integrations.social.meta import MetaConnector
from app.integrations.social.x import XConnector


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
    if name == XConnector.provider:
        return XConnector(client_id=client_id, client_secret=client_secret)
    if name == MetaConnector.provider:
        return MetaConnector(client_id=client_id, client_secret=client_secret)
    raise UnknownSocialProviderError(f"unknown social provider: {provider!r}")


__all__ = [
    "AuthorisedPage",
    "LinkedInConnector",
    "MediaRequiredError",
    "MetaConnector",
    "OAuthRevokedError",
    "PostResult",
    "SocialConnector",
    "SocialConnectorError",
    "SocialPost",
    "UnknownSocialProviderError",
    "XConnector",
    "build_social_connector",
]
