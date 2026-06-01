"""W43 — Apollo client unit tests (respx-mocked).

Covers:
  * Disabled (no API key) raises EnrichmentDisabledError.
  * 200 with `person` → EnrichedPerson populated.
  * 200 with empty body → returns None (no match).
  * 404 → returns None (no match).
  * 401/500 → raises EnrichmentRequestError with status_code.
  * Timeout → raises EnrichmentRequestError.
  * merge_into_payload preserves CSV-provided values; only fills gaps.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.integrations.apollo import (
    ApolloClient,
    EnrichedPerson,
    EnrichmentDisabledError,
    EnrichmentRequestError,
)


def test_apollo_client_rejects_missing_api_key() -> None:
    with pytest.raises(EnrichmentDisabledError):
        ApolloClient(api_key="")


@respx.mock
async def test_match_person_returns_enriched_record() -> None:
    respx.post("https://api.apollo.io/api/v1/people/match").mock(
        return_value=httpx.Response(
            200,
            json={
                "person": {
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "title": "VP of Engineering",
                    "seniority": "vp",
                    "linkedin_url": "https://linkedin.com/in/adalovelace",
                    "headline": "Counting machines",
                    "country": "United Kingdom",
                    "organization": {
                        "name": "Analytical Engine Co.",
                        "industry": "Computing",
                    },
                }
            },
        )
    )
    client = ApolloClient(api_key="test-key")
    person = await client.match_person(email="ada@analyticalengine.example")
    assert person is not None
    assert person.title == "VP of Engineering"
    assert person.seniority == "vp"
    assert person.linkedin_url == "https://linkedin.com/in/adalovelace"
    assert person.company == "Analytical Engine Co."
    assert person.industry == "Computing"
    assert person.country == "GB"


@respx.mock
async def test_match_person_returns_none_on_empty_body() -> None:
    respx.post("https://api.apollo.io/api/v1/people/match").mock(
        return_value=httpx.Response(200, json={"person": None})
    )
    client = ApolloClient(api_key="test-key")
    assert await client.match_person(email="ghost@nowhere.test") is None


@respx.mock
async def test_match_person_returns_none_on_404() -> None:
    respx.post("https://api.apollo.io/api/v1/people/match").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    client = ApolloClient(api_key="test-key")
    assert await client.match_person(email="ghost@nowhere.test") is None


@respx.mock
async def test_match_person_raises_on_auth_error() -> None:
    respx.post("https://api.apollo.io/api/v1/people/match").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    client = ApolloClient(api_key="test-key")
    with pytest.raises(EnrichmentRequestError) as exc:
        await client.match_person(email="a@b.test")
    assert exc.value.status_code == 401


@respx.mock
async def test_match_person_raises_on_server_error() -> None:
    respx.post("https://api.apollo.io/api/v1/people/match").mock(
        return_value=httpx.Response(500, text="boom")
    )
    client = ApolloClient(api_key="test-key")
    with pytest.raises(EnrichmentRequestError) as exc:
        await client.match_person(email="a@b.test")
    assert exc.value.status_code == 500


@respx.mock
async def test_match_person_raises_on_timeout() -> None:
    respx.post("https://api.apollo.io/api/v1/people/match").mock(
        side_effect=httpx.TimeoutException("slow")
    )
    client = ApolloClient(api_key="test-key")
    with pytest.raises(EnrichmentRequestError):
        await client.match_person(email="a@b.test")


def test_merge_into_payload_prefers_csv_values() -> None:
    apollo = EnrichedPerson(
        title="VP Engineering",
        company="Acme",
        first_name="A. Lovelace",  # less complete than CSV value
        linkedin_url="https://linkedin.com/in/ada",
    )
    payload = {
        "email": "ada@acme.test",
        "first_name": "Ada",  # CSV had this
        "last_name": "Lovelace",
    }
    merged = apollo.merge_into_payload(payload)
    assert merged["first_name"] == "Ada"  # CSV value wins
    assert merged["title"] == "VP Engineering"  # Apollo fills the gap
    assert merged["company"] == "Acme"
    assert merged["linkedin_url"] == "https://linkedin.com/in/ada"
    assert merged["email"] == "ada@acme.test"
