"""HubSpot OAuth + Contacts API connector (W11, E12-S01).

Implements the `CrmConnector` interface against HubSpot's public OAuth + CRM v3
API. Tested with respx-mocked endpoints; a live integration smoke test against
a real HubSpot developer account is a follow-up.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from urllib.parse import urlencode

import httpx

from app.integrations.base import CrmConnector, CrmRecord, OAuthTokens

_HTTP_TIMEOUT = httpx.Timeout(10.0)


def _parse_hs_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # HubSpot returns ISO-8601 with trailing 'Z'; datetime.fromisoformat accepts
    # 'YYYY-MM-DDTHH:MM:SS+00:00' but not the 'Z' form prior to py3.11.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HubSpotConnector(CrmConnector):
    provider: ClassVar[str] = "hubspot"
    default_scopes: ClassVar[tuple[str, ...]] = (
        "crm.objects.contacts.read",
        "crm.objects.companies.read",
    )

    AUTHORIZE_URL: ClassVar[str] = "https://app.hubspot.com/oauth/authorize"
    TOKEN_URL: ClassVar[str] = "https://api.hubapi.com/oauth/v1/token"
    CONTACTS_URL: ClassVar[str] = "https://api.hubapi.com/crm/v3/objects/contacts"

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
            "scope": " ".join(scopes or self.default_scopes),
            "state": state,
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthTokens:
        data = await self._post_token(
            {
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            }
        )
        return self._tokens_from_response(data)

    async def refresh(self, *, refresh_token: str) -> OAuthTokens:
        data = await self._post_token(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
            }
        )
        return self._tokens_from_response(data)

    async def list_contacts(
        self,
        *,
        access_token: str,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[list[CrmRecord], str | None]:
        params: dict[str, str] = {"limit": str(limit)}
        if after:
            params["after"] = after
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                self.CONTACTS_URL,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        records = [
            CrmRecord(
                external_id=str(r["id"]),
                properties=dict(r.get("properties", {})),
                updated_at=_parse_hs_datetime(r.get("properties", {}).get("hs_lastmodifieddate")),
            )
            for r in payload.get("results", [])
        ]
        next_after = payload.get("paging", {}).get("next", {}).get("after")
        return records, next_after

    # --- internals ---------------------------------------------------------

    async def _post_token(self, form: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                self.TOKEN_URL,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())

    def _tokens_from_response(self, data: dict[str, Any]) -> OAuthTokens:
        expires_in = int(data.get("expires_in", 0))
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=list(self.default_scopes),
        )
