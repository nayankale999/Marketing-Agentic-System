"""Bulk-enrich an audience by filling missing personalisation fields
from Apollo (W43).

A member is "enrichable" when its payload is missing any of:
  * title
  * linkedin_url
  * seniority
  * company (some CSVs ship only first_name + email)

Members with all four already populated are skipped — saves Apollo
credits and avoids overwriting good CSV data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AudienceMember
from app.integrations.apollo import (
    ApolloClient,
    EnrichmentRequestError,
)


_ENRICHMENT_TARGET_FIELDS: tuple[str, ...] = (
    "title",
    "linkedin_url",
    "seniority",
    "company",
)


@dataclass(frozen=True)
class EnrichmentSummary:
    """Per-call summary so the assistant can narrate the result."""

    audience_id: UUID
    total_members: int
    already_complete: int
    enriched: int
    not_found: int
    failed: int
    errors: list[str] = field(default_factory=list)


def _needs_enrichment(payload: dict[str, Any]) -> bool:
    return any(not payload.get(f) for f in _ENRICHMENT_TARGET_FIELDS)


async def enrich_audience(
    session: AsyncSession,
    *,
    audience_id: UUID,
    apollo: ApolloClient,
    max_to_enrich: int = 500,
) -> EnrichmentSummary:
    """For every member missing personalisation fields, call Apollo and
    merge the response into payload. We cap at `max_to_enrich` to bound
    Apollo cost per invocation; the assistant can call again to continue.

    Failures on individual rows are recorded in the summary's errors
    list and do NOT abort the rest of the batch — getting some
    enrichment beats getting none, and Apollo's free tier often hits
    intermittent 429s.
    """
    rows = (
        await session.execute(
            select(AudienceMember).where(AudienceMember.audience_id == audience_id)
        )
    ).scalars().all()

    summary = EnrichmentSummary(
        audience_id=audience_id,
        total_members=len(rows),
        already_complete=0,
        enriched=0,
        not_found=0,
        failed=0,
    )
    enriched_count = 0
    now = datetime.now(UTC)

    for member in rows:
        payload = dict(member.payload or {})
        if not _needs_enrichment(payload):
            summary = _bump(summary, already_complete=1)
            continue
        if enriched_count >= max_to_enrich:
            break

        try:
            person = await apollo.match_person(
                email=payload.get("email") or member.external_id,
                first_name=payload.get("first_name"),
                last_name=payload.get("last_name"),
                company=payload.get("company"),
            )
        except EnrichmentRequestError as exc:
            summary = _bump(summary, failed=1, error=str(exc))
            continue

        if person is None:
            summary = _bump(summary, not_found=1)
            continue

        merged = person.merge_into_payload(payload)
        member.payload = merged
        member.fetched_at = now
        member.source = (member.source or "csv") + "+apollo"
        await session.flush()
        enriched_count += 1
        summary = _bump(summary, enriched=1)

    return summary


def _bump(
    summary: EnrichmentSummary,
    *,
    already_complete: int = 0,
    enriched: int = 0,
    not_found: int = 0,
    failed: int = 0,
    error: str | None = None,
) -> EnrichmentSummary:
    """Frozen-dataclass-friendly tally bump. We rebuild rather than
    mutate to keep EnrichmentSummary safely shareable."""
    errors = list(summary.errors)
    if error:
        errors.append(error[:200])
    return EnrichmentSummary(
        audience_id=summary.audience_id,
        total_members=summary.total_members,
        already_complete=summary.already_complete + already_complete,
        enriched=summary.enriched + enriched,
        not_found=summary.not_found + not_found,
        failed=summary.failed + failed,
        errors=errors,
    )


__all__ = ["EnrichmentSummary", "enrich_audience"]
