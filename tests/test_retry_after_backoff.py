"""W31 — `Retry-After` header honoring (E08-S06 #3).

Tests the `_parse_retry_after` helper in the LinkedIn connector. The
actual retry loop with backoff is already covered in test_linkedin_connector;
this file focuses on the parser side: integer seconds, HTTP-date form,
malformed input, missing header."""

from __future__ import annotations

from email.utils import format_datetime
from datetime import UTC, datetime, timedelta

from app.integrations.social.linkedin import _parse_retry_after


def test_parse_retry_after_integer_seconds() -> None:
    assert _parse_retry_after("30") == 30.0


def test_parse_retry_after_float_seconds() -> None:
    assert _parse_retry_after("1.5") == 1.5


def test_parse_retry_after_negative_clamps_to_zero() -> None:
    # Spec says non-negative seconds — defensively clamp.
    assert _parse_retry_after("-5") == 0.0


def test_parse_retry_after_http_date_future() -> None:
    """Future HTTP-date returns the seconds-until-target (within ~2s)."""
    target = datetime.now(UTC) + timedelta(seconds=10)
    header = format_datetime(target, usegmt=True)
    delta = _parse_retry_after(header)
    assert delta is not None
    assert 8 <= delta <= 12  # tolerant for execution time


def test_parse_retry_after_http_date_past_clamps_to_zero() -> None:
    """If the provided HTTP-date is in the past, clamp to 0 rather than
    return a negative wait."""
    target = datetime.now(UTC) - timedelta(seconds=30)
    header = format_datetime(target, usegmt=True)
    assert _parse_retry_after(header) == 0.0


def test_parse_retry_after_none_returns_none() -> None:
    assert _parse_retry_after(None) is None


def test_parse_retry_after_empty_string_returns_none() -> None:
    assert _parse_retry_after("") is None
    assert _parse_retry_after("   ") is None


def test_parse_retry_after_garbage_returns_none() -> None:
    assert _parse_retry_after("not-a-number") is None
    assert _parse_retry_after("monday at 3pm") is None


# ---------------------------------------------------------------------------
# Integration: a 429 with Retry-After is observed by the publish_post loop
# ---------------------------------------------------------------------------


import httpx
import pytest
import respx

from app.integrations.social.base import SocialPost
from app.integrations.social.linkedin import LinkedInConnector


_UGC_URL = LinkedInConnector.UGC_POSTS_URL


def _connector() -> LinkedInConnector:
    return LinkedInConnector(client_id="cid", client_secret="csec")


@respx.mock
async def test_publish_honors_retry_after_header_then_succeeds() -> None:
    """A 429 with a tiny Retry-After value is honored — second attempt
    succeeds. Real retry timing isn't asserted (jittered sleep is hard to
    pin); we verify both attempts happened."""
    route = respx.post(_UGC_URL).mock(
        side_effect=[
            httpx.Response(
                429, text="slow down", headers={"Retry-After": "0"}
            ),
            httpx.Response(201, json={"id": "urn:li:share:retry"}),
        ]
    )
    result = await _connector().publish_post(
        access_token="tok",
        page_urn="urn:li:organization:1",
        post=SocialPost(text="x"),
    )
    assert route.call_count == 2
    assert result.provider_post_id == "urn:li:share:retry"
