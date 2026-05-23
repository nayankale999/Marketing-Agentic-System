"""Ad platform integrations (W40, E12-S04 scaffolding).

Google Ads + Meta Ads connectors. W40 ships the OAuth surface + the
abstract base — the actual campaign / ad-set / spend ingestion calls
raise `NotImplementedError` and are left for a follow-up.
"""

from app.integrations.ads.base import (
    AdAccount,
    AdConnector,
    AdConnectorError,
    AdCampaign,
    AdCampaignUpsertResult,
    AdSet,
    AdSetUpsertResult,
    PlatformAdState,
    SpendRecord,
    UnknownAdProviderError,
)
from app.integrations.ads.google import GoogleAdsConnector
from app.integrations.ads.meta import MetaAdsConnector


def build_ad_connector(
    provider: str,
    *,
    client_id: str,
    client_secret: str,
) -> AdConnector:
    """Resolve a provider name to a concrete ad-platform connector."""
    name = (provider or "").strip().lower()
    if name == GoogleAdsConnector.provider:
        return GoogleAdsConnector(client_id=client_id, client_secret=client_secret)
    if name == MetaAdsConnector.provider:
        return MetaAdsConnector(client_id=client_id, client_secret=client_secret)
    raise UnknownAdProviderError(f"unknown ad provider: {provider!r}")


__all__ = [
    "AdAccount",
    "AdConnector",
    "AdConnectorError",
    "AdCampaign",
    "AdCampaignUpsertResult",
    "AdSet",
    "AdSetUpsertResult",
    "GoogleAdsConnector",
    "MetaAdsConnector",
    "PlatformAdState",
    "SpendRecord",
    "UnknownAdProviderError",
    "build_ad_connector",
]
