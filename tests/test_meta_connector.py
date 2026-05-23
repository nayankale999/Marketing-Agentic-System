"""W40 — MetaConnector tests (E12-S03).

respx-mocked coverage of the Meta (Facebook Pages) connector.
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
from app.integrations.social.meta import MetaConnector

_TOKEN_URL = MetaConnector.TOKEN_URL
_ME_ACCOUNTS_URL = MetaConnector.ME_ACCOUNTS_URL


def _connector() -> MetaConnector:
    return MetaConnector(client_id="cid-meta", client_secret="csec-meta")


# ---------------------------------------------------------------------------
# authorize_url
# ---------------------------------------------------------------------------


def test_authorize_url_has_meta_scopes() -> None:
    url = _connector().authorize_url(state="s", redirect_uri="http://cb")
    assert url.startswith(MetaConnector.AUTHORIZE_URL)
    assert "pages_manage_posts" in url
    assert "client_id=cid-meta" in url


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


@respx.mock
async def test_exchange_code_promotes_to_long_lived() -> None:
    # Short-lived token first, then the fb_exchange_token call.
    route = respx.get(_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "short-A", "expires_in": 3600}),
            httpx.Response(
                200,
                json={"access_token": "long-A", "expires_in": 5184000},
            ),
        ]
    )
    tokens = await _connector().exchange_code(code="c1", redirect_uri="http://cb")
    assert tokens.access_token == "long-A"
    assert route.call_count == 2


@respx.mock
async def test_exchange_code_falls_back_to_short_lived_on_promote_failure() -> None:
    respx.get(_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "short-A", "expires_in": 3600}),
            httpx.Response(500, text="upgrade failed"),
        ]
    )
    tokens = await _connector().exchange_code(code="c1", redirect_uri="http://cb")
    assert tokens.access_token == "short-A"


async def test_refresh_tokens_always_revoked() -> None:
    with pytest.raises(OAuthRevokedError):
        await _connector().refresh_tokens(refresh_token="ignored")


@respx.mock
async def test_exchange_code_400_is_provider_rejected() -> None:
    respx.get(_TOKEN_URL).mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(ProviderRejectedError):
        await _connector().exchange_code(code="c", redirect_uri="http://cb")


# ---------------------------------------------------------------------------
# list_authorised_pages
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_authorised_pages_returns_page_tokens() -> None:
    respx.get(_ME_ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "page-1",
                        "name": "Acme Inc",
                        "access_token": "page-tok-1",
                    },
                    {
                        "id": "page-2",
                        "name": "Acme Side",
                        "access_token": "page-tok-2",
                    },
                ]
            },
        )
    )
    pages = await _connector().list_authorised_pages(access_token="user-tok")
    assert {p.page_id for p in pages} == {"page-1", "page-2"}
    assert all(p.extra["page_token"].startswith("page-tok") for p in pages)


@respx.mock
async def test_list_authorised_pages_oauth_exception_is_revoked() -> None:
    respx.get(_ME_ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            400,
            text='{"error":{"type":"OAuthException","message":"expired"}}',
        )
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().list_authorised_pages(access_token="user-tok")


# ---------------------------------------------------------------------------
# publish_post
# ---------------------------------------------------------------------------


@respx.mock
async def test_publish_post_happy_path() -> None:
    page_id = "page-1"
    respx.post(MetaConnector.feed_url(page_id)).mock(
        return_value=httpx.Response(200, json={"id": "post-789"})
    )
    result = await _connector().publish_post(
        access_token="page-tok-1",
        page_urn=page_id,
        post=SocialPost(text="hello world"),
    )
    assert result.provider_post_id == "post-789"
    assert "post-789" in result.url


@respx.mock
async def test_publish_post_includes_link_when_media_url_present() -> None:
    page_id = "page-1"
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"id": "post-link"})

    respx.post(MetaConnector.feed_url(page_id)).mock(side_effect=_capture)
    await _connector().publish_post(
        access_token="page-tok-1",
        page_urn=page_id,
        post=SocialPost(text="check this", media_url="https://example.com/article"),
    )
    assert "link=" in captured["body"]


async def test_publish_post_media_required_without_url_raises() -> None:
    with pytest.raises(MediaRequiredError):
        await _connector().publish_post(
            access_token="page-tok-1",
            page_urn="page-1",
            post=SocialPost(text="x", media_required=True),
        )


@respx.mock
async def test_publish_post_401_is_oauth_revoked() -> None:
    page_id = "page-1"
    respx.post(MetaConnector.feed_url(page_id)).mock(
        return_value=httpx.Response(401, text="revoked")
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().publish_post(
            access_token="page-tok",
            page_urn=page_id,
            post=SocialPost(text="hi"),
        )


@respx.mock
async def test_publish_post_retries_on_5xx() -> None:
    page_id = "page-1"
    route = respx.post(MetaConnector.feed_url(page_id)).mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(200, json={"id": "post-r"}),
        ]
    )
    result = await _connector().publish_post(
        access_token="page-tok",
        page_urn=page_id,
        post=SocialPost(text="retry me"),
    )
    assert result.provider_post_id == "post-r"
    assert route.call_count == 2
