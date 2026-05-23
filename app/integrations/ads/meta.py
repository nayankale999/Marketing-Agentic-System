"""Meta Ads connector (W40 scaffolding, E12-S04).

OAuth + ad-account listing wired against Meta's Marketing API. Campaign
/ ad-set / spend / state methods are inherited from the base and raise
`NotImplementedError` — full implementation is a follow-up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar
from urllib.parse import urlencode

import httpx

from app.integrations.ads.base import (
    AdAccount,
    AdConnector,
    OAuthRevokedError,
    OAuthTokens,
    ProviderRejectedError,
    ProviderUnreachableError,
)

_HTTP_TIMEOUT = httpx.Timeout(10.0)


class MetaAdsConnector(AdConnector):
    provider: ClassVar[str] = "meta_ads"
    default_scopes: ClassVar[tuple[str, ...]] = (
        "ads_management",
        "ads_read",
        "business_management",
    )

    GRAPH_VERSION: ClassVar[str] = "v18.0"
    AUTHORIZE_URL: ClassVar[str] = "https://www.facebook.com/v18.0/dialog/oauth"
    TOKEN_URL: ClassVar[str] = "https://graph.facebook.com/v18.0/oauth/access_token"
    ME_ADACCOUNTS_URL: ClassVar[str] = "https://graph.facebook.com/v18.0/me/adaccounts"

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    def authorize_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        scopes: list[str] | None = None,
    ) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(scopes or self.default_scopes),
            "response_type": "code",
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> OAuthTokens:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.TOKEN_URL,
                    params={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                        "code": code,
                    },
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"meta_ads token transport failure: {exc}", provider=self.provider
            ) from exc
        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"meta_ads token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"meta_ads token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        body = resp.json()
        expires_in = int(body.get("expires_in") or 3600)
        return OAuthTokens(
            access_token=str(body["access_token"]),
            refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=list(self.default_scopes),
        )

    async def list_ad_accounts(
        self, *, access_token: str
    ) -> list[AdAccount]:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.ME_ADACCOUNTS_URL,
                    params={
                        "access_token": access_token,
                        "fields": "id,name,currency",
                    },
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"meta_ads me/adaccounts transport failure: {exc}",
                provider=self.provider,
            ) from exc
        if resp.status_code in (401, 403):
            raise OAuthRevokedError(
                f"meta_ads me/adaccounts {resp.status_code}", provider=self.provider
            )
        if resp.status_code == 400 and "OAuthException" in resp.text:
            raise OAuthRevokedError(
                f"meta_ads OAuthException: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"meta_ads me/adaccounts {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"meta_ads me/adaccounts {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )

        accounts: list[AdAccount] = []
        for item in resp.json().get("data", []) or []:
            account_id = str(item.get("id") or "")
            if not account_id:
                continue
            accounts.append(
                AdAccount(
                    account_id=account_id,
                    name=str(item.get("name") or account_id),
                    currency=str(item.get("currency") or ""),
                )
            )
        return accounts


__all__ = ["MetaAdsConnector"]
