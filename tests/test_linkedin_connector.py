"""W30 — LinkedInConnector tests (E12-S03).

Cover the connector in isolation via respx-mocked endpoints. The OAuth
flow + API are exercised by test_integrations_social.py; this file
focuses on:

  * authorize_url assembly
  * exchange_code + refresh_tokens happy + revoked paths
  * list_authorised_pages — multi-page response normalisation
  * publish_post — text-only happy path, retry on 5xx + 429, revoke on
    401, media_required precondition, transport failure → unreachable
"""

from __future__ import annotations

import json

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
from app.integrations.social.linkedin import LinkedInConnector

_TOKEN_URL = LinkedInConnector.TOKEN_URL
_ACLS_URL = LinkedInConnector.ACLS_URL
_ORG_URL_PREFIX = LinkedInConnector.ORGANIZATIONS_URL
_UGC_URL = LinkedInConnector.UGC_POSTS_URL


def _connector() -> LinkedInConnector:
    return LinkedInConnector(client_id="cid-test", client_secret="csec-test")


# ---------------------------------------------------------------------------
# authorize_url
# ---------------------------------------------------------------------------


def test_authorize_url_includes_required_params() -> None:
    url = _connector().authorize_url(
        state="state-abc",
        redirect_uri="http://localhost/cb",
        scopes=["r_liteprofile"],
    )
    assert url.startswith(LinkedInConnector.AUTHORIZE_URL)
    assert "response_type=code" in url
    assert "client_id=cid-test" in url
    assert "state=state-abc" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%2Fcb" in url
    assert "scope=r_liteprofile" in url


def test_authorize_url_defaults_to_default_scopes() -> None:
    url = _connector().authorize_url(state="s", redirect_uri="http://cb")
    # Default scopes include w_organization_social so we can publish.
    assert "w_organization_social" in url


# ---------------------------------------------------------------------------
# OAuth token endpoints
# ---------------------------------------------------------------------------


@respx.mock
async def test_exchange_code_happy_path() -> None:
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "tok-A",
                "refresh_token": "tok-R",
                "expires_in": 5184000,  # 60 days
                "scope": "r_liteprofile w_organization_social",
            },
        )
    )
    tokens = await _connector().exchange_code(code="xyz", redirect_uri="http://cb")
    assert tokens.access_token == "tok-A"
    assert tokens.refresh_token == "tok-R"
    assert "w_organization_social" in tokens.scopes
    assert tokens.expires_at is not None


@respx.mock
async def test_exchange_code_4xx_raises_rejected() -> None:
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "bad code"}
        )
    )
    with pytest.raises(ProviderRejectedError):
        await _connector().exchange_code(code="bad", redirect_uri="http://cb")


@respx.mock
async def test_refresh_tokens_revoked_raises_oauth_revoked() -> None:
    """Refresh token rejected (invalid_grant) → OAuthRevokedError."""
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "revoked"}
        )
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().refresh_tokens(refresh_token="dead-token")


@respx.mock
async def test_token_endpoint_5xx_unreachable() -> None:
    respx.post(_TOKEN_URL).mock(return_value=httpx.Response(503, text="boom"))
    with pytest.raises(ProviderUnreachableError):
        await _connector().exchange_code(code="x", redirect_uri="http://cb")


# ---------------------------------------------------------------------------
# list_authorised_pages
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_authorised_pages_returns_admin_orgs() -> None:
    respx.get(_ACLS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {"organization": "urn:li:organization:111", "role": "ADMINISTRATOR"},
                    {"organization": "urn:li:organization:222", "role": "ADMINISTRATOR"},
                ]
            },
        )
    )
    respx.get(f"{_ORG_URL_PREFIX}/111").mock(
        return_value=httpx.Response(200, json={"localizedName": "Acme Inc"})
    )
    respx.get(f"{_ORG_URL_PREFIX}/222").mock(
        return_value=httpx.Response(200, json={"localizedName": "Globex"})
    )

    pages = await _connector().list_authorised_pages(access_token="tok")
    assert {p.page_name for p in pages} == {"Acme Inc", "Globex"}
    assert all(p.urn.startswith("urn:li:organization:") for p in pages)


@respx.mock
async def test_list_pages_skips_unresolvable_orgs() -> None:
    respx.get(_ACLS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {"organization": "urn:li:organization:111", "role": "ADMINISTRATOR"},
                    {"organization": "urn:li:organization:999", "role": "ADMINISTRATOR"},
                ]
            },
        )
    )
    respx.get(f"{_ORG_URL_PREFIX}/111").mock(
        return_value=httpx.Response(200, json={"localizedName": "Acme"})
    )
    respx.get(f"{_ORG_URL_PREFIX}/999").mock(return_value=httpx.Response(404))

    pages = await _connector().list_authorised_pages(access_token="tok")
    assert len(pages) == 1
    assert pages[0].page_name == "Acme"


@respx.mock
async def test_list_pages_401_surfaces_oauth_revoked() -> None:
    respx.get(_ACLS_URL).mock(
        return_value=httpx.Response(401, json={"message": "revoked"})
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().list_authorised_pages(access_token="dead")


# ---------------------------------------------------------------------------
# publish_post
# ---------------------------------------------------------------------------


@respx.mock
async def test_publish_post_happy_path_returns_urn_and_url() -> None:
    respx.post(_UGC_URL).mock(
        return_value=httpx.Response(
            201,
            json={"id": "urn:li:share:7000000000000000000"},
        )
    )
    result = await _connector().publish_post(
        access_token="tok",
        page_urn="urn:li:organization:111",
        post=SocialPost(text="Hello LinkedIn"),
    )
    assert result.provider_post_id == "urn:li:share:7000000000000000000"
    assert "urn:li:share:7000000000000000000" in result.url


@respx.mock
async def test_publish_post_falls_back_to_x_restli_id_header() -> None:
    """Some LinkedIn responses return the URN in the X-RestLi-Id header
    rather than the body — connector should pick it up either way."""
    respx.post(_UGC_URL).mock(
        return_value=httpx.Response(
            201, json={}, headers={"X-RestLi-Id": "urn:li:share:42"}
        )
    )
    result = await _connector().publish_post(
        access_token="tok",
        page_urn="urn:li:organization:111",
        post=SocialPost(text="x"),
    )
    assert result.provider_post_id == "urn:li:share:42"


@respx.mock
async def test_publish_post_retries_on_429_then_succeeds() -> None:
    route = respx.post(_UGC_URL).mock(
        side_effect=[
            httpx.Response(429, text="rate limited"),
            httpx.Response(201, json={"id": "urn:li:share:99"}),
        ]
    )
    result = await _connector().publish_post(
        access_token="tok",
        page_urn="urn:li:organization:111",
        post=SocialPost(text="x"),
    )
    assert route.call_count == 2
    assert result.provider_post_id == "urn:li:share:99"


@respx.mock
async def test_publish_post_5xx_then_5xx_then_succeeds() -> None:
    """Retries up to 2 times — 3rd attempt succeeds."""
    route = respx.post(_UGC_URL).mock(
        side_effect=[
            httpx.Response(503, text="boom"),
            httpx.Response(502, text="boom"),
            httpx.Response(201, json={"id": "urn:li:share:55"}),
        ]
    )
    result = await _connector().publish_post(
        access_token="tok",
        page_urn="urn:li:organization:111",
        post=SocialPost(text="x"),
    )
    assert route.call_count == 3
    assert result.provider_post_id == "urn:li:share:55"


@respx.mock
async def test_publish_post_exhausts_retries_and_surfaces_unreachable() -> None:
    respx.post(_UGC_URL).mock(return_value=httpx.Response(503, text="boom"))
    with pytest.raises(ProviderUnreachableError):
        await _connector().publish_post(
            access_token="tok",
            page_urn="urn:li:organization:111",
            post=SocialPost(text="x"),
        )


@respx.mock
async def test_publish_post_401_surfaces_oauth_revoked() -> None:
    respx.post(_UGC_URL).mock(
        return_value=httpx.Response(401, json={"message": "revoked"})
    )
    with pytest.raises(OAuthRevokedError):
        await _connector().publish_post(
            access_token="tok",
            page_urn="urn:li:organization:111",
            post=SocialPost(text="x"),
        )


@respx.mock
async def test_publish_post_4xx_other_surfaces_rejected() -> None:
    respx.post(_UGC_URL).mock(return_value=httpx.Response(422, text="bad shape"))
    with pytest.raises(ProviderRejectedError):
        await _connector().publish_post(
            access_token="tok",
            page_urn="urn:li:organization:111",
            post=SocialPost(text="x"),
        )


async def test_media_required_without_url_raises() -> None:
    """Pre-call validation: media_required=True + no media_url → precondition."""
    with pytest.raises(MediaRequiredError):
        await _connector().publish_post(
            access_token="tok",
            page_urn="urn:li:organization:111",
            post=SocialPost(text="x", media_required=True),
        )


@respx.mock
async def test_publish_post_body_shape() -> None:
    """Confirm we send the UGC body the way LinkedIn expects."""
    route = respx.post(_UGC_URL).mock(
        return_value=httpx.Response(201, json={"id": "urn:li:share:1"})
    )
    await _connector().publish_post(
        access_token="tok",
        page_urn="urn:li:organization:111",
        post=SocialPost(text="Hello world"),
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["author"] == "urn:li:organization:111"
    assert sent["lifecycleState"] == "PUBLISHED"
    assert (
        sent["specificContent"]["com.linkedin.ugc.ShareContent"]["shareCommentary"]["text"]
        == "Hello world"
    )
    assert (
        sent["specificContent"]["com.linkedin.ugc.ShareContent"]["shareMediaCategory"]
        == "NONE"
    )
