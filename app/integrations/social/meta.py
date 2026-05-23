"""Meta (Facebook Pages) social connector (W40, E12-S03).

Meta's Graph API uses OAuth 2.0 with two token tiers:
  * Short-lived user token from the OAuth exchange
  * Long-lived page tokens fetched via `GET /me/accounts` after grant

For publishing we use the page token (not the user token) — that's what
the Graph API requires for `/{page_id}/feed` POST.

What's in scope for W40:
  * OAuth 2.0: `authorize_url` + `exchange_code` (no `refresh_tokens` —
    Meta uses long-lived tokens that expire after ~60 days and require
    re-OAuth, not a refresh grant)
  * `list_authorised_pages` → /me/accounts, returns Page rows including
    their per-page access_token (stashed in `extra` so the dispatcher
    can use the page token rather than the user token)
  * `publish_post` → POST /{page_id}/feed with `{message}` (or `link` if
    media_url looks like a URL)

What's deferred:
  * Instagram publishing (uses Graph API but requires extra business-
    account linking and Container objects)
  * Photo/video uploads via the dedicated /photos and /videos endpoints
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from app.integrations.social.base import (
    AuthorisedPage,
    MediaRequiredError,
    OAuthRevokedError,
    OAuthTokens,
    PostResult,
    ProviderRejectedError,
    ProviderUnreachableError,
    SocialConnector,
    SocialPost,
)

_HTTP_TIMEOUT = httpx.Timeout(10.0)
_MAX_RETRIES = 2
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_REVOKED_STATUS = frozenset({401, 403})


class MetaConnector(SocialConnector):
    provider: ClassVar[str] = "meta"
    default_scopes: ClassVar[tuple[str, ...]] = (
        "pages_show_list",
        "pages_manage_posts",
        "pages_read_engagement",
    )

    GRAPH_VERSION: ClassVar[str] = "v18.0"
    AUTHORIZE_URL: ClassVar[str] = "https://www.facebook.com/v18.0/dialog/oauth"
    TOKEN_URL: ClassVar[str] = "https://graph.facebook.com/v18.0/oauth/access_token"
    ME_ACCOUNTS_URL: ClassVar[str] = "https://graph.facebook.com/v18.0/me/accounts"

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    @classmethod
    def feed_url(cls, page_id: str) -> str:
        return f"https://graph.facebook.com/{cls.GRAPH_VERSION}/{page_id}/feed"

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

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
        # Step 1: short-lived user token via authorization_code grant.
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
                f"meta token transport failure: {exc}", provider=self.provider
            ) from exc

        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"meta token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"meta token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )

        short = resp.json()
        access_token = str(short.get("access_token") or "")
        if not access_token:
            raise ProviderRejectedError(
                "meta token: no access_token in response", provider=self.provider
            )
        expires_in = int(short.get("expires_in") or 3600)

        # Step 2: exchange for a long-lived token (~60 day expiry).
        long_lived = await self._exchange_for_long_lived(access_token)
        return long_lived or OAuthTokens(
            access_token=access_token,
            refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=list(self.default_scopes),
        )

    async def _exchange_for_long_lived(
        self, short_token: str
    ) -> OAuthTokens | None:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.TOKEN_URL,
                    params={
                        "grant_type": "fb_exchange_token",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "fb_exchange_token": short_token,
                    },
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
            return None
        if resp.status_code >= 400:
            return None
        body = resp.json()
        token = str(body.get("access_token") or short_token)
        expires_in = int(body.get("expires_in") or 60 * 24 * 3600)  # default 60d
        return OAuthTokens(
            access_token=token,
            refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=list(self.default_scopes),
        )

    async def refresh_tokens(self, *, refresh_token: str) -> OAuthTokens:
        # Meta does not issue refresh tokens. Long-lived tokens must be
        # re-OAuthed when they expire. Surface as revoked so callers route
        # the user through the OAuth start.
        raise OAuthRevokedError(
            "meta does not support refresh tokens; re-OAuth required",
            provider=self.provider,
        )

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    async def list_authorised_pages(
        self, *, access_token: str
    ) -> list[AuthorisedPage]:
        """Returns one row per Page the user can publish on. Each row's
        `extra["page_token"]` carries the page-scoped token the dispatcher
        uses to publish — using the user token would 403."""
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.ME_ACCOUNTS_URL,
                    params={"access_token": access_token, "fields": "id,name,access_token"},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"meta me/accounts transport failure: {exc}", provider=self.provider
            ) from exc

        self._raise_for_status(resp, where="me/accounts")
        pages: list[AuthorisedPage] = []
        for item in resp.json().get("data", []):
            page_id = str(item.get("id") or "")
            if not page_id:
                continue
            pages.append(
                AuthorisedPage(
                    page_id=page_id,
                    page_name=str(item.get("name") or page_id),
                    urn=page_id,
                    extra={
                        "page_token": str(item.get("access_token") or ""),
                    },
                )
            )
        return pages

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish_post(
        self,
        *,
        access_token: str,
        page_urn: str,
        post: SocialPost,
    ) -> PostResult:
        """Publish a feed post. The `access_token` parameter is expected
        to be the **page-scoped** token (from `AuthorisedPage.extra
        ["page_token"]`), not the user token. `page_urn` is the page id.
        """
        if post.media_required and not post.media_url:
            raise MediaRequiredError(
                "post.media_required=true but no media_url supplied",
                provider=self.provider,
            )

        body: dict[str, Any] = {"message": post.text}
        if post.media_url:
            # Treat the media URL as a link share — Meta's link unfurl
            # surfaces it as a card. Photo/video upload is a follow-up.
            body["link"] = post.media_url

        attempt = 0
        backoff_seconds = 0.1
        url = self.feed_url(page_urn)
        while True:
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    resp = await client.post(
                        url,
                        data={**body, "access_token": access_token},
                    )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                if attempt >= _MAX_RETRIES:
                    raise ProviderUnreachableError(
                        f"meta transport failure: {exc}", provider=self.provider
                    ) from exc
                await self._sleep_jittered(backoff_seconds)
                backoff_seconds *= 2
                attempt += 1
                continue

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                wait_seconds = retry_after if retry_after is not None else backoff_seconds
                await self._sleep_jittered(wait_seconds)
                backoff_seconds *= 2
                attempt += 1
                continue

            self._raise_for_status(resp, where="feed")
            data = resp.json()
            post_id = str(data.get("id") or "")
            if not post_id:
                raise ProviderRejectedError(
                    "meta feed returned no id", provider=self.provider
                )
            return PostResult(
                provider_post_id=post_id,
                url=f"https://www.facebook.com/{post_id}",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _raise_for_status(self, resp: httpx.Response, *, where: str) -> None:
        if resp.status_code < 400:
            return
        if resp.status_code in _REVOKED_STATUS:
            raise OAuthRevokedError(
                f"meta {where} {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        # Meta returns 400 with `OAuthException` codes for revoked tokens.
        if resp.status_code == 400 and "OAuthException" in resp.text:
            raise OAuthRevokedError(
                f"meta {where} OAuthException: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"meta {where} {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        raise ProviderRejectedError(
            f"meta {where} {resp.status_code}: {resp.text[:200]}",
            provider=self.provider,
        )

    @staticmethod
    async def _sleep_jittered(base: float) -> None:
        await asyncio.sleep(base + random.uniform(0, base * 0.5))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value.strip()), 0.0)
    except (TypeError, ValueError):
        return None


__all__ = ["MetaConnector"]
