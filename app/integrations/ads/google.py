"""Google Ads connector (W40 scaffolding, E12-S04).

OAuth + ad-account listing wired against the Google OAuth 2.0 endpoints
and the Google Ads API's customer-listing call. Campaign / ad-set /
spend / state methods are inherited from the base and raise
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


class GoogleAdsConnector(AdConnector):
    provider: ClassVar[str] = "google_ads"
    default_scopes: ClassVar[tuple[str, ...]] = (
        "https://www.googleapis.com/auth/adwords",
        "openid",
        "email",
    )

    AUTHORIZE_URL: ClassVar[str] = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL: ClassVar[str] = "https://oauth2.googleapis.com/token"
    CUSTOMERS_URL: ClassVar[str] = (
        "https://googleads.googleapis.com/v16/customers:listAccessibleCustomers"
    )

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
            "scope": " ".join(scopes or self.default_scopes),
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> OAuthTokens:
        return await self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        )

    async def _post_token(self, data: dict[str, str]) -> OAuthTokens:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(self.TOKEN_URL, data=data)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"google_ads token transport failure: {exc}", provider=self.provider
            ) from exc
        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"google_ads token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"google_ads token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        body = resp.json()
        expires_in = int(body.get("expires_in") or 3600)
        return OAuthTokens(
            access_token=str(body["access_token"]),
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=list(self.default_scopes),
        )

    async def list_ad_accounts(
        self, *, access_token: str
    ) -> list[AdAccount]:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.CUSTOMERS_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"google_ads customers transport failure: {exc}",
                provider=self.provider,
            ) from exc
        if resp.status_code in (401, 403):
            raise OAuthRevokedError(
                f"google_ads customers {resp.status_code}", provider=self.provider
            )
        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"google_ads customers {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"google_ads customers {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )

        # Response: {"resourceNames": ["customers/1234567890", ...]}
        accounts: list[AdAccount] = []
        for resource in resp.json().get("resourceNames", []) or []:
            # Resource format: "customers/{id}"
            account_id = str(resource).split("/")[-1]
            if not account_id:
                continue
            accounts.append(
                AdAccount(
                    account_id=account_id,
                    name=f"Customer {account_id}",
                    currency="",  # Resolved per-customer in the follow-up
                    extra={"resource_name": resource},
                )
            )
        return accounts


__all__ = ["GoogleAdsConnector"]
