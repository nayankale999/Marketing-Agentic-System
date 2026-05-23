"""W40 — Ad platform connector scaffolding tests (E12-S04).

The OAuth + account-listing surface is wired against the providers'
real endpoints. Campaign / ad-set / spend / state methods are
scaffolded with NotImplementedError until the follow-up unit.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from app.integrations.ads import build_ad_connector
from app.integrations.ads.base import (
    AdCampaign,
    AdSet,
    OAuthRevokedError,
    ProviderRejectedError,
    UnknownAdProviderError,
)
from app.integrations.ads.google import GoogleAdsConnector
from app.integrations.ads.meta import MetaAdsConnector


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_resolves_google_and_meta() -> None:
    g = build_ad_connector("google_ads", client_id="c", client_secret="s")
    m = build_ad_connector("meta_ads", client_id="c", client_secret="s")
    assert isinstance(g, GoogleAdsConnector)
    assert isinstance(m, MetaAdsConnector)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(UnknownAdProviderError):
        build_ad_connector("nope", client_id="c", client_secret="s")


# ---------------------------------------------------------------------------
# Google Ads — OAuth + list
# ---------------------------------------------------------------------------


def test_google_authorize_url_requests_offline_access() -> None:
    url = GoogleAdsConnector(client_id="c", client_secret="s").authorize_url(
        state="s1", redirect_uri="http://cb"
    )
    assert url.startswith(GoogleAdsConnector.AUTHORIZE_URL)
    assert "access_type=offline" in url
    assert "prompt=consent" in url


@respx.mock
async def test_google_exchange_code_happy_path() -> None:
    respx.post(GoogleAdsConnector.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "g-tok",
                "refresh_token": "g-ref",
                "expires_in": 3600,
            },
        )
    )
    conn = GoogleAdsConnector(client_id="c", client_secret="s")
    tokens = await conn.exchange_code(code="abc", redirect_uri="http://cb")
    assert tokens.access_token == "g-tok"
    assert tokens.refresh_token == "g-ref"


@respx.mock
async def test_google_list_accounts_parses_resource_names() -> None:
    respx.get(GoogleAdsConnector.CUSTOMERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "resourceNames": [
                    "customers/1111111111",
                    "customers/2222222222",
                ]
            },
        )
    )
    conn = GoogleAdsConnector(client_id="c", client_secret="s")
    accounts = await conn.list_ad_accounts(access_token="g-tok")
    assert {a.account_id for a in accounts} == {"1111111111", "2222222222"}


@respx.mock
async def test_google_list_accounts_401_is_revoked() -> None:
    respx.get(GoogleAdsConnector.CUSTOMERS_URL).mock(
        return_value=httpx.Response(401, text="unauth")
    )
    conn = GoogleAdsConnector(client_id="c", client_secret="s")
    with pytest.raises(OAuthRevokedError):
        await conn.list_ad_accounts(access_token="g-tok")


# ---------------------------------------------------------------------------
# Meta Ads — OAuth + list
# ---------------------------------------------------------------------------


def test_meta_ads_authorize_url_has_business_management_scope() -> None:
    url = MetaAdsConnector(client_id="c", client_secret="s").authorize_url(
        state="s", redirect_uri="http://cb"
    )
    assert "business_management" in url


@respx.mock
async def test_meta_ads_exchange_code_happy_path() -> None:
    respx.get(MetaAdsConnector.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "m-tok", "expires_in": 7200}
        )
    )
    conn = MetaAdsConnector(client_id="c", client_secret="s")
    tokens = await conn.exchange_code(code="abc", redirect_uri="http://cb")
    assert tokens.access_token == "m-tok"


@respx.mock
async def test_meta_ads_list_accounts_returns_currency() -> None:
    respx.get(MetaAdsConnector.ME_ADACCOUNTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "act_1", "name": "Acme Ads", "currency": "USD"},
                ]
            },
        )
    )
    conn = MetaAdsConnector(client_id="c", client_secret="s")
    accounts = await conn.list_ad_accounts(access_token="m-tok")
    assert accounts[0].account_id == "act_1"
    assert accounts[0].currency == "USD"


@respx.mock
async def test_meta_ads_list_accounts_oauth_exception_is_revoked() -> None:
    respx.get(MetaAdsConnector.ME_ADACCOUNTS_URL).mock(
        return_value=httpx.Response(
            400,
            text='{"error":{"type":"OAuthException","message":"expired"}}',
        )
    )
    conn = MetaAdsConnector(client_id="c", client_secret="s")
    with pytest.raises(OAuthRevokedError):
        await conn.list_ad_accounts(access_token="m-tok")


# ---------------------------------------------------------------------------
# Mutating methods raise NotImplementedError (W40 scaffolding contract)
# ---------------------------------------------------------------------------


async def test_upsert_campaign_not_implemented_on_google() -> None:
    conn = GoogleAdsConnector(client_id="c", client_secret="s")
    with pytest.raises(NotImplementedError):
        await conn.upsert_campaign(
            access_token="t",
            account_id="acc",
            campaign=AdCampaign(
                name="x",
                daily_budget=Decimal("10"),
                start_date=date.today(),
                end_date=None,
            ),
        )


async def test_upsert_ad_set_not_implemented_on_meta_ads() -> None:
    conn = MetaAdsConnector(client_id="c", client_secret="s")
    with pytest.raises(NotImplementedError):
        await conn.upsert_ad_set(
            access_token="t",
            account_id="acc",
            ad_set=AdSet(
                campaign_id="c1",
                name="x",
                targeting={},
            ),
        )


async def test_fetch_spend_not_implemented() -> None:
    for conn in (
        GoogleAdsConnector(client_id="c", client_secret="s"),
        MetaAdsConnector(client_id="c", client_secret="s"),
    ):
        with pytest.raises(NotImplementedError):
            await conn.fetch_spend(
                access_token="t",
                account_id="acc",
                since=date.today(),
                until=date.today(),
            )
