"""Apollo.io enrichment client (W43, outbound personalisation).

Wraps Apollo's `/people/match` endpoint, which takes a thin identifier
(email is the strongest signal) and returns the full person record:
title, seniority, LinkedIn URL, employer, industry, etc. We use it to
fill the gaps on CSV-uploaded contacts whose payload is sparse — the
SDR exports a list from somewhere with `email` only and Apollo backfills
the personalisation slots.

Failure modes (handled, not raised):
  * Missing API key → `EnrichmentDisabledError` (caller chooses to skip
    or report).
  * 404 (Apollo found nobody) → returns `None`; the AudienceMember stays
    as-is. We do NOT consider this an error.
  * HTTP 4xx / 5xx → `EnrichmentRequestError` with status + body excerpt.
  * Timeout → `EnrichmentRequestError("timeout")`.

The module is pure I/O — no DB, no business logic. The caller decides
how to merge the returned fields into AudienceMember.payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class EnrichmentError(Exception):
    """Base for enrichment failures."""


class EnrichmentDisabledError(EnrichmentError):
    """No Apollo API key configured."""


class EnrichmentRequestError(EnrichmentError):
    """Apollo returned a non-success status, or the request failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class EnrichedPerson:
    """Subset of the Apollo person payload we care about for outbound
    personalisation. All fields optional — Apollo may know some but
    not others."""

    title: str | None = None
    seniority: str | None = None
    linkedin_url: str | None = None
    company: str | None = None
    industry: str | None = None
    country: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None

    def merge_into_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a new payload dict with Apollo's fields filled in
        where the original payload was missing or empty. We never
        overwrite a value the uploader provided — the CSV is the source
        of truth where it has data."""
        out = dict(payload)
        for field_name, value in self.__dict__.items():
            if value and not out.get(field_name):
                out[field_name] = value
        return out


def _coerce_country(raw: str | None) -> str | None:
    """Apollo returns long-form country names ('United States'). The
    CSV path expects ISO-3166-1 alpha-2. Best-effort: map a handful of
    common ones, otherwise leave the raw value and let the upstream
    validator filter it out if it's strict."""
    if not raw:
        return None
    mapping = {
        "united states": "US",
        "united kingdom": "GB",
        "canada": "CA",
        "australia": "AU",
        "germany": "DE",
        "france": "FR",
        "india": "IN",
        "singapore": "SG",
        "ireland": "IE",
        "netherlands": "NL",
    }
    return mapping.get(raw.strip().lower(), raw if len(raw) == 2 else None)


def _person_from_apollo(person: dict[str, Any]) -> EnrichedPerson:
    """Translate Apollo's response shape into our internal dataclass.
    Apollo's payload is large; we lift only the personalisation-relevant
    fields. See https://docs.apollo.io/reference/people-match."""
    org = person.get("organization") or {}
    return EnrichedPerson(
        title=person.get("title"),
        seniority=person.get("seniority"),
        linkedin_url=person.get("linkedin_url"),
        company=org.get("name") or person.get("organization_name"),
        industry=org.get("industry") or person.get("industry"),
        country=_coerce_country(person.get("country")),
        first_name=person.get("first_name"),
        last_name=person.get("last_name"),
        headline=person.get("headline"),
    )


class ApolloClient:
    """Thin async wrapper around Apollo's REST API.

    Holds the API key + base URL. Each method returns a typed result
    (or raises an EnrichmentError subclass). Pass an explicit
    `httpx.AsyncClient` for tests (respx-mocked); leave None for
    production (we build one with the configured timeout per call)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.apollo.io/api/v1",
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise EnrichmentDisabledError(
                "Apollo API key not configured (set APOLLO_API_KEY)."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._http_client = http_client

    async def match_person(
        self,
        *,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
        company: str | None = None,
    ) -> EnrichedPerson | None:
        """Look up a person in Apollo. Returns the enriched record, or
        `None` if Apollo couldn't match. Raises `EnrichmentRequestError`
        on transport / 5xx failures so the caller can decide whether to
        retry the whole batch.

        Email alone is enough for Apollo's match endpoint, but we pass
        first/last/company too when available — improves match rate
        for shared inboxes / catch-all addresses."""
        params: dict[str, Any] = {"email": email}
        if first_name:
            params["first_name"] = first_name
        if last_name:
            params["last_name"] = last_name
        if company:
            params["organization_name"] = company

        url = f"{self._base_url}/people/match"
        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self._api_key,
        }
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    url, headers=headers, json=params, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, headers=headers, json=params)
        except httpx.TimeoutException as exc:
            raise EnrichmentRequestError("apollo request timed out") from exc
        except httpx.HTTPError as exc:
            raise EnrichmentRequestError(f"apollo request failed: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise EnrichmentRequestError(
                f"apollo returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        body = resp.json()
        person = body.get("person")
        if not person:
            # Apollo sometimes returns 200 with an empty body when no
            # match was found — treat the same as 404.
            return None
        return _person_from_apollo(person)


__all__ = [
    "ApolloClient",
    "EnrichedPerson",
    "EnrichmentDisabledError",
    "EnrichmentError",
    "EnrichmentRequestError",
]
