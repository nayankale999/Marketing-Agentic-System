"""Bulk-ingest aggregated analytic events with dedup + UTM attribution.

The writer is provider-agnostic — connectors translate their payload into
`WebAnalyticsEvent` and pass the list here. We:
  - resolve UTM → campaign_id (NULL if no match)
  - persist each row, ON CONFLICT DO NOTHING on (tenant_id, provider_event_id)
  - report counts: imported / duplicates / unattributed
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.db.models import AnalyticEvent
from app.integrations.web_analytics.attribution import resolve_campaign_by_utm
from app.integrations.web_analytics.base import WebAnalyticsEvent


@dataclass(frozen=True)
class IngestSummary:
    imported: int
    duplicates: int
    unattributed: int


async def ingest_events(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    events: list[WebAnalyticsEvent],
) -> IngestSummary:
    """Persist events with dedup + UTM attribution. Caller commits the tx."""
    if not events:
        return IngestSummary(imported=0, duplicates=0, unattributed=0)

    # Resolve UTM -> campaign_id with one lookup per distinct utm_campaign.
    cache: dict[str, UUID | None] = {}
    for ev in events:
        key = (ev.utm_campaign or "").strip().lower()
        if ev.utm_campaign and key not in cache:
            cache[key] = await resolve_campaign_by_utm(
                session, tenant_id=tenant_id, utm_campaign=ev.utm_campaign
            )

    rows: list[dict[str, Any]] = []
    unattributed = 0
    for ev in events:
        key = (ev.utm_campaign or "").strip().lower()
        campaign_id = cache.get(key) if ev.utm_campaign else None
        if ev.utm_campaign and campaign_id is None:
            unattributed += 1
        payload = dict(ev.payload)
        if ev.utm_campaign:
            payload.setdefault("utm_campaign", ev.utm_campaign)
        if ev.utm_source:
            payload.setdefault("utm_source", ev.utm_source)
        if ev.utm_medium:
            payload.setdefault("utm_medium", ev.utm_medium)

        rows.append(
            {
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "event_type": ev.event_type,
                "metric_value": ev.metric_value,
                "payload": payload,
                "provider_event_id": ev.provider_event_id,
                "event_at": ev.event_at,
            }
        )

    stmt = (
        pg_insert(AnalyticEvent)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "provider_event_id"],
            index_where=text("provider_event_id IS NOT NULL"),
        )
        .returning(AnalyticEvent.id)
    )
    inserted_ids = (await session.execute(stmt)).scalars().all()
    imported = len(inserted_ids)
    duplicates = len(events) - imported
    return IngestSummary(imported=imported, duplicates=duplicates, unattributed=unattributed)


async def count_events_for_tenant(session: AsyncSession, tenant_id: UUID) -> int:
    """Small helper used by tests + the dashboard."""
    from sqlalchemy import func

    return (
        await session.execute(
            select(func.count())
            .select_from(AnalyticEvent)
            .where(AnalyticEvent.tenant_id == tenant_id)
        )
    ).scalar_one()
