"""LinkedIn social connector (W30, E12-S03 + E11-S05).

LinkedIn uses OAuth 2.0 (3-legged) with offline access for refresh tokens
and the v2 UGC Posts API for publishing.

What's in scope for W30:
  * `authorize_url` + `exchange_code` + `refresh_tokens`
  * `list_authorised_pages` via `/v2/organizationAcls?q=roleAssignee` —
    returns the org pages this user is allowed to publish on behalf of
  * `publish_post` with TEXT-ONLY content via `/v2/ugcPosts`

What's deferred:
  * Image/video upload — multi-step protocol (`registerUpload` →
    `uploadBinary` → `Reference`) that's its own work unit
  * Personal-profile (member) sharing — only organization shares
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
_MAX_RETRIES = 2  # E11-S05 #2: tool retries up to 2 times on retryable codes
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_REVOKED_STATUS = frozenset({401, 403})


class LinkedInConnector(SocialConnector):
    provider: ClassVar[str] = "linkedin"
    default_scopes: ClassVar[tuple[str, ...]] = (
        "r_liteprofile",
        "r_organization_social",
        "w_organization_social",
        "rw_organization_admin",
    )

    AUTHORIZE_URL: ClassVar[str] = "https://www.linkedin.com/oauth/v2/authorization"
    TOKEN_URL: ClassVar[str] = "https://www.linkedin.com/oauth/v2/accessToken"
    ACLS_URL: ClassVar[str] = "https://api.linkedin.com/v2/organizationAcls"
    UGC_POSTS_URL: ClassVar[str] = "https://api.linkedin.com/v2/ugcPosts"
    ORGANIZATIONS_URL: ClassVar[str] = "https://api.linkedin.com/v2/organizations"

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

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
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes or self.default_scopes),
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

    async def refresh_tokens(self, *, refresh_token: str) -> OAuthTokens:
        try:
            return await self._post_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                }
            )
        except ProviderRejectedError as exc:
            # LinkedIn returns 400 with `invalid_grant` when the refresh
            # token is revoked. Surface as OAuthRevokedError so callers
            # know re-OAuth is required.
            raise OAuthRevokedError(
                f"linkedin refresh failed: {exc}", provider=self.provider
            ) from exc

    async def _post_token(self, data: dict[str, str]) -> OAuthTokens:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    self.TOKEN_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"linkedin token transport failure: {exc}", provider=self.provider
            ) from exc

        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"linkedin token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 400:
            raise ProviderRejectedError(
                f"linkedin token {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        body = resp.json()
        expires_in = int(body.get("expires_in") or 0)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in or 3600)
        scopes_raw = body.get("scope") or ""
        scopes = [s for s in scopes_raw.split() if s]
        return OAuthTokens(
            access_token=str(body["access_token"]),
            refresh_token=body.get("refresh_token"),
            expires_at=expires_at,
            scopes=scopes,
        )

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    async def list_authorised_pages(
        self, *, access_token: str
    ) -> list[AuthorisedPage]:
        """Pull the organisation pages the OAuthed user can publish on
        behalf of. LinkedIn's `organizationAcls` endpoint returns ACL
        rows; for each ACL we fetch the org's display name."""
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.ACLS_URL,
                    params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
                    headers=_auth_headers(access_token),
                )
                self._raise_for_status(resp, where="organizationAcls")
                acls = resp.json().get("elements", [])

                pages: list[AuthorisedPage] = []
                for acl in acls:
                    org_urn = acl.get("organization") or acl.get("organizationalTarget")
                    if not isinstance(org_urn, str) or not org_urn.startswith("urn:li:organization:"):
                        continue
                    org_id = org_urn.split(":")[-1]
                    name_resp = await client.get(
                        f"{self.ORGANIZATIONS_URL}/{org_id}",
                        headers=_auth_headers(access_token),
                    )
                    if name_resp.status_code >= 400:
                        # Skip ones we can't resolve a name for rather than blow up the whole list.
                        continue
                    payload = name_resp.json()
                    name = (
                        payload.get("localizedName")
                        or payload.get("name")
                        or f"Organization {org_id}"
                    )
                    pages.append(
                        AuthorisedPage(
                            page_id=org_id,
                            page_name=str(name),
                            urn=org_urn,
                            extra={"role": "ADMINISTRATOR"},
                        )
                    )
                return pages
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnreachableError(
                f"linkedin pages transport failure: {exc}", provider=self.provider
            ) from exc

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
        """Publish a UGC text post on behalf of the given org. Retries
        retryable status codes up to `_MAX_RETRIES` times with jittered
        backoff per E11-S05 #2."""
        if post.media_required and not post.media_url:
            raise MediaRequiredError(
                "post.media_required=true but no media_url supplied",
                provider=self.provider,
            )

        body = self._build_ugc_body(page_urn=page_urn, post=post)

        attempt = 0
        backoff_seconds = 0.1
        while True:
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    resp = await client.post(
                        self.UGC_POSTS_URL,
                        json=body,
                        headers={
                            **_auth_headers(access_token),
                            "X-Restli-Protocol-Version": "2.0.0",
                            "Content-Type": "application/json",
                        },
                    )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                if attempt >= _MAX_RETRIES:
                    raise ProviderUnreachableError(
                        f"linkedin transport failure: {exc}", provider=self.provider
                    ) from exc
                await self._sleep_jittered(backoff_seconds)
                backoff_seconds *= 2
                attempt += 1
                continue

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                # E08-S06 #3: honor `Retry-After` on 429 if present.
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                wait_seconds = (
                    retry_after if retry_after is not None else backoff_seconds
                )
                await self._sleep_jittered(wait_seconds)
                backoff_seconds *= 2
                attempt += 1
                continue

            self._raise_for_status(resp, where="ugcPosts")
            data = resp.json()
            urn = str(
                data.get("id")
                or resp.headers.get("X-RestLi-Id")
                or ""
            )
            if not urn:
                raise ProviderRejectedError(
                    "linkedin returned no post id", provider=self.provider
                )
            return PostResult(
                provider_post_id=urn,
                url=_share_url(urn),
            )

    def _build_ugc_body(self, *, page_urn: str, post: SocialPost) -> dict[str, Any]:
        visibility = {"com.linkedin.ugc.MemberNetworkVisibility": post.visibility}
        share_content: dict[str, Any] = {
            "shareCommentary": {"text": post.text},
            "shareMediaCategory": "NONE",
        }
        # Media support is text-only in W30 — `media_url` is accepted for the
        # AC validation only. Real upload protocol lands later.
        return {
            "author": page_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": share_content,
            },
            "visibility": visibility,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _raise_for_status(self, resp: httpx.Response, *, where: str) -> None:
        if resp.status_code < 400:
            return
        if resp.status_code in _REVOKED_STATUS:
            raise OAuthRevokedError(
                f"linkedin {where} {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        if resp.status_code >= 500:
            raise ProviderUnreachableError(
                f"linkedin {where} {resp.status_code}: {resp.text[:200]}",
                provider=self.provider,
            )
        raise ProviderRejectedError(
            f"linkedin {where} {resp.status_code}: {resp.text[:200]}",
            provider=self.provider,
        )

    @staticmethod
    async def _sleep_jittered(base: float) -> None:
        await asyncio.sleep(base + random.uniform(0, base * 0.5))


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _share_url(urn: str) -> str:
    """LinkedIn doesn't publish a stable per-share URL via the API, but the
    feed URL pattern works for org shares: /feed/update/<urn>/."""
    return f"https://www.linkedin.com/feed/update/{urn}/"


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value (W31, E08-S06 #3).

    The header can be either an integer number of seconds OR an HTTP-date
    timestamp. Returns `None` if the value is missing or unparseable so
    the caller falls back to its default backoff."""
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    # Seconds form (most providers, including LinkedIn).
    try:
        seconds = float(stripped)
        return max(seconds, 0.0)
    except ValueError:
        pass
    # HTTP-date form: parse and compute delta.
    from email.utils import parsedate_to_datetime

    try:
        target = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    delta = (target - now).total_seconds()
    return max(delta, 0.0)


__all__ = ["LinkedInConnector"]
