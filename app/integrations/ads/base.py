"""Ad-platform connector base classes (W40, E12-S04).

Shape modeled on the social `SocialConnector` ABC but adapted to the
ad-platform workflow:

  * OAuth flow — same `authorize_url` + `exchange_code` + (optional)
    `refresh_tokens` surface
  * `list_ad_accounts(access_token)` — picker after OAuth
  * `upsert_campaign(...)` — create-or-update the platform's campaign
    record when MAS launches a campaign with paid channels
  * `upsert_ad_set(...)` — same for ad sets / ad groups
  * `fetch_spend(...)` — nightly spend ingest (feeds `analytic_event`
    rows of `event_type='spend'`, E10-S06)
  * `fetch_platform_state(...)` — webhook/poll for paused campaigns,
    disapproved creatives, etc.

W40 ships the abstractions + OAuth wiring. The mutating + spend-ingest
calls raise `NotImplementedError` so the API surface is stable for a
follow-up implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar


class AdConnectorError(Exception):
    """Base for ad-platform errors."""

    provider: str = ""

    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider or self.provider


class OAuthRevokedError(AdConnectorError):
    """Token revoked or expired beyond refresh."""


class ProviderUnreachableError(AdConnectorError):
    """Transport failure or 5xx — caller should retry."""


class ProviderRejectedError(AdConnectorError):
    """4xx other than 401 — input needs to change before retry."""


class UnknownAdProviderError(AdConnectorError):
    """Raised by the factory for unsupported provider names."""


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdAccount:
    account_id: str
    name: str
    currency: str
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdCampaign:
    """Inputs to `upsert_campaign`. Provider-agnostic."""

    name: str
    daily_budget: Decimal
    start_date: date
    end_date: date | None
    status: str = "PAUSED"  # safe default — caller activates explicitly
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdSet:
    """Inputs to `upsert_ad_set`."""

    campaign_id: str
    name: str
    targeting: dict[str, object]
    daily_budget: Decimal | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdCampaignUpsertResult:
    platform_campaign_id: str
    is_new: bool


@dataclass(frozen=True)
class AdSetUpsertResult:
    platform_ad_set_id: str
    is_new: bool


@dataclass(frozen=True)
class SpendRecord:
    """One day's spend for one platform campaign."""

    day: date
    platform_campaign_id: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PlatformAdState:
    """A snapshot of platform-side campaign state — what we read in
    `fetch_platform_state` to reconcile against MAS records."""

    platform_campaign_id: str
    status: str
    disapprovals: list[str] = field(default_factory=list)


class AdConnector(ABC):
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
    async def list_ad_accounts(
        self, *, access_token: str
    ) -> list[AdAccount]: ...

    # The four below are scaffolded — concrete connectors raise
    # NotImplementedError until the follow-up implementation lands.

    async def upsert_campaign(
        self,
        *,
        access_token: str,
        account_id: str,
        campaign: AdCampaign,
        platform_campaign_id: str | None = None,
    ) -> AdCampaignUpsertResult:
        raise NotImplementedError(
            f"{self.provider}.upsert_campaign is deferred to a follow-up"
        )

    async def upsert_ad_set(
        self,
        *,
        access_token: str,
        account_id: str,
        ad_set: AdSet,
        platform_ad_set_id: str | None = None,
    ) -> AdSetUpsertResult:
        raise NotImplementedError(
            f"{self.provider}.upsert_ad_set is deferred to a follow-up"
        )

    async def fetch_spend(
        self,
        *,
        access_token: str,
        account_id: str,
        since: date,
        until: date,
    ) -> list[SpendRecord]:
        raise NotImplementedError(
            f"{self.provider}.fetch_spend is deferred to a follow-up"
        )

    async def fetch_platform_state(
        self,
        *,
        access_token: str,
        account_id: str,
        platform_campaign_ids: list[str],
    ) -> list[PlatformAdState]:
        raise NotImplementedError(
            f"{self.provider}.fetch_platform_state is deferred to a follow-up"
        )
