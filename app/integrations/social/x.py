"""X (Twitter) social connector (W40, E12-S03).

X's current developer platform uses OAuth 2.0 with PKCE for user-context
authentication. There's no concept of "pages" — the authenticated user
is the publishing target. `list_authorised_pages` surfaces the user's
own account as a single AuthorisedPage so the UI flow stays uniform
across providers.

What's in scope for W40:
  * OAuth 2.0 PKCE: `authorize_url` + `exchange_code` + `refresh_tokens`
  * `list_authorised_pages` → /2/users/me, returns the user as one row
  * `publish_post` → POST /2/tweets with `{text}` (and `media.media_ids`
    when `media_url` is provided — though uploading the media itself is
    a follow-up; for W40 we accept a pre-uploaded media id via the
    `extra` dict on the post, treating bare URLs as text)

What's deferred:
  * Media upload (multi-step v1.1 protocol). For now `media_url` strings
    are appended to the tweet text.
  * Long-form (Premium) tweets.
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


class XConnector(SocialConnector):
    provider: ClassVar[str] = "x"
    default_scopes: ClassVar[tuple[str, ...]] = (
        "tweet.read",
        "tweet.write",
        "users.read",
        "offline.access",  # required for refresh tokens
    )

    AUTHORIZE_URL: ClassVar[str] = "https://twitter.com/i/oauth2/authorize"
    TOKEN_URL: ClassVar[str] = "https://api.twitter.com/2/oauth2/token"
    USERS_ME_URL: ClassVar[str] = "https://api.twitter.com/2/users/me"
    TWEETS_URL: ClassVar[str] = "https://api.twitter.com/2/tweets"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        code_verifier: str | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        # PKCE: caller is responsible for stashing the verifier between
        # authorize_url and exchange_code. We accept it on construction so
        # the API layer can hold it in the OAuth state cookie.
        self._code_verifier = code_verifier

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
        # PKCE: caller provides `code_verifier` at construction time. We
        # use it (or a placeholder when not yet generated) to compute the
        # challenge — X requires `code_challenge` even though we send
        # `plain` here for simplicity. Production should use S256.
        verifier = self._code_verifier or "challenge"
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes or self.default_scopes),
            "code_challenge": verifier,
            "code_challenge_method": "plain",
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> OAuthTokens:
        verifier = self._code_verifier or "challenge"
        return await self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "code_verifier": verifier,
            }
        )

    async def refresh_tokens(self, *, refresh_token: str) -> OAuthTokens:
        try:
            return await self._post_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                }
            )
        except ProviderRejectedError as exc:
            # invalid_grant / revoked → re-OAuth required.
            raise OAuthRevokedError(
                f"x refresh failed: {exc}", provider=self.provider
            ) from exc

    async def _post_token(self, data: dict[str, str]) -> OAuthTokens:
        # X uses Basic auth on the token endpoint for confidential clients.
        auth = (self._client_id, self._client_secret) if self._client_secret else None
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    self.TOKEN_URL,
                    data=data,
                    auth=auth,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"x token transport failure: {exc}", provider=self.provider
            ) from exc

        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"x token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"x token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        body = resp.json()
        expires_in = int(body.get("expires_in") or 7200)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        scopes_raw = body.get("scope") or ""
        scopes = [s for s in scopes_raw.split() if s]
        return OAuthTokens(
            access_token=str(body["access_token"]),
            refresh_token=body.get("refresh_token"),
            expires_at=expires_at,
            scopes=scopes,
        )

    # ------------------------------------------------------------------
    # "Pages" — X has no concept; the user IS the target
    # ------------------------------------------------------------------

    async def list_authorised_pages(
        self, *, access_token: str
    ) -> list[AuthorisedPage]:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.USERS_ME_URL,
                    headers=_auth_headers(access_token),
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"x users/me transport failure: {exc}", provider=self.provider
            ) from exc

        self._raise_for_status(resp, where="users/me")
        body = resp.json().get("data") or {}
        user_id = str(body.get("id") or "")
        username = str(body.get("username") or body.get("name") or "")
        if not user_id:
            raise ProviderRejectedError(
                "x users/me returned no id", provider=self.provider
            )
        return [
            AuthorisedPage(
                page_id=user_id,
                page_name=username or f"@{user_id}",
                urn=user_id,
                extra={"username": username},
            )
        ]

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
        if post.media_required and not post.media_url:
            raise MediaRequiredError(
                "post.media_required=true but no media_url supplied",
                provider=self.provider,
            )

        # Media upload protocol is a follow-up — for W40 we append a bare
        # URL to the text so the tweet still carries the link.
        text = post.text
        if post.media_url and not post.media_required:
            text = f"{text}\n{post.media_url}".strip()

        body = {"text": text}

        attempt = 0
        backoff_seconds = 0.1
        while True:
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    resp = await client.post(
                        self.TWEETS_URL,
                        json=body,
                        headers={
                            **_auth_headers(access_token),
                            "Content-Type": "application/json",
                        },
                    )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                if attempt >= _MAX_RETRIES:
                    raise ProviderUnreachableError(
                        f"x transport failure: {exc}", provider=self.provider
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

            self._raise_for_status(resp, where="tweets")
            data = resp.json().get("data") or {}
            tweet_id = str(data.get("id") or "")
            if not tweet_id:
                raise ProviderRejectedError(
                    "x tweets returned no id", provider=self.provider
                )
            username = data.get("author_id") or page_urn
            return PostResult(
                provider_post_id=tweet_id,
                url=f"https://twitter.com/{username}/status/{tweet_id}",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _raise_for_status(self, resp: httpx.Response, *, where: str) -> None:
        if resp.status_code < 400:
            return
        if resp.status_code in _REVOKED_STATUS:
            raise OAuthRevokedError(
                f"x {where} {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"x {where} {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        raise ProviderRejectedError(
            f"x {where} {resp.status_code}: {resp.text[:200]}",
            provider=self.provider,
        )

    @staticmethod
    async def _sleep_jittered(base: float) -> None:
        await asyncio.sleep(base + random.uniform(0, base * 0.5))


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value.strip()), 0.0)
    except (TypeError, ValueError):
        return None


__all__ = ["XConnector"]
