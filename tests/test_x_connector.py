"""W40 — XConnector tests (E12-S03).

respx-mocked coverage of the X (Twitter) connector. Mirrors the shape
of test_linkedin_connector.py.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.integrations.social.base import (
    MediaRequiredError,
    OAuthRevokedError,
    ProviderRejectedError,
    ProviderUnreachableError,
    SocialPost,
)
from app.integrations.social.x import XConnector

_TOKEN_URL = XConnector.TOKEN_URL
_USERS_ME_URL = XConnector.USERS_ME_URL
_TWEETS_URL = XConnector.TWEETS_URL


def _connector() -> XConnector:
    return XConnector(
        client_id="cid-x",
        client_secret="csec-x",
        code_verifier="v-abc",
    )


# ---------------------------------------------------------------------------
# authorize_url
# ---------------------------------------------------------------------------


def test_authorize_url_includes_pkce_challenge() -> None:
    url = _connector().authorize_url(
        state="state-abc",
        redirect_uri="http://localhost/cb",
    )
    assert url.startswith(XConnector.AUTHORIZE_URL)
    assert "code_challenge=v-abc" in url
    assert "code_challenge_method=plain" in url
    assert "client_id=cid-x" in url


def test_authorize_url_uses_default_scopes_when_unset() -> None:
    url = _connector().authorize_url(state="s", redirect_uri="http://cb")
    assert "tweet.write" in url
    assert "offline.access" in url


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


@respx.mock
async def test_exchange_code_happy_path() -> None:
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "tok-A",
                "refresh_token": "tok-R",
                "expires_in": 7200,
                "scope": "tweet.read tweet.write offline.access",
            },
        )
    )
    tokens = await _connector().exchange_code(code="c1", redirect_uri="http://cb")
    assert tokens.access_token == "tok-A"
    assert tokens.refresh_token == "tok-R"
    assert "tweet.write" in tokens.scopes


@respx.mock
async def test_refresh_tokens_revoked_surfaces_oauth_revoked() -> None:
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().refresh_tokens(refresh_token="tok-R")


@respx.mock
async def test_exchange_code_5xx_is_unreachable() -> None:
    respx.post(_TOKEN_URL).mock(return_value=httpx.Response(503, text="busy"))
    with pytest.raises(ProviderUnreachableError):
        await _connector().exchange_code(code="c", redirect_uri="http://cb")


# ---------------------------------------------------------------------------
# list_authorised_pages
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_authorised_pages_returns_self_user() -> None:
    respx.get(_USERS_ME_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": "user-123", "username": "acme_co"}},
        )
    )
    pages = await _connector().list_authorised_pages(access_token="tok-A")
    assert len(pages) == 1
    assert pages[0].page_id == "user-123"
    assert pages[0].page_name == "acme_co"
    assert pages[0].extra["username"] == "acme_co"


@respx.mock
async def test_list_authorised_pages_401_raises_revoked() -> None:
    respx.get(_USERS_ME_URL).mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().list_authorised_pages(access_token="tok-A")


# ---------------------------------------------------------------------------
# publish_post
# ---------------------------------------------------------------------------


@respx.mock
async def test_publish_post_happy_path() -> None:
    respx.post(_TWEETS_URL).mock(
        return_value=httpx.Response(
            201, json={"data": {"id": "tweet-9", "text": "hi"}}
        )
    )
    result = await _connector().publish_post(
        access_token="tok-A",
        page_urn="user-123",
        post=SocialPost(text="Hello world"),
    )
    assert result.provider_post_id == "tweet-9"
    assert "/status/tweet-9" in result.url


@respx.mock
async def test_publish_post_appends_media_url_to_text() -> None:
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"data": {"id": "tweet-x"}})

    respx.post(_TWEETS_URL).mock(side_effect=_capture)
    await _connector().publish_post(
        access_token="tok-A",
        page_urn="user-123",
        post=SocialPost(text="Look", media_url="https://img.example/cat.jpg"),
    )
    assert "Look" in captured["body"]
    assert "cat.jpg" in captured["body"]


async def test_publish_post_media_required_without_url_raises() -> None:
    with pytest.raises(MediaRequiredError):
        await _connector().publish_post(
            access_token="tok-A",
            page_urn="user-123",
            post=SocialPost(text="x", media_required=True),
        )


@respx.mock
async def test_publish_post_401_is_oauth_revoked() -> None:
    respx.post(_TWEETS_URL).mock(
        return_value=httpx.Response(401, text="revoked")
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().publish_post(
            access_token="tok-A",
            page_urn="user-123",
            post=SocialPost(text="hi"),
        )


@respx.mock
async def test_publish_post_400_is_provider_rejected() -> None:
    respx.post(_TWEETS_URL).mock(
        return_value=httpx.Response(400, text="duplicate")
    )
    with pytest.raises(ProviderRejectedError):
        await _connector().publish_post(
            access_token="tok-A",
            page_urn="user-123",
            post=SocialPost(text="hi"),
        )


@respx.mock
async def test_publish_post_retries_on_429() -> None:
    route = respx.post(_TWEETS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(201, json={"data": {"id": "tweet-retry"}}),
        ]
    )
    result = await _connector().publish_post(
        access_token="tok-A",
        page_urn="user-123",
        post=SocialPost(text="retry-me"),
    )
    assert result.provider_post_id == "tweet-retry"
    assert route.call_count == 2
