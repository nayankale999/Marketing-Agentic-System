"""Per-campaign KPI rollup (W34, E10-S01).

Aggregates `analytic_event` rows belonging to a campaign — directly
(events with `campaign_id` set, e.g. Plausible after UTM attribution) or
indirectly (SendGrid events whose `payload->>'sg_message_id'` matches a
`dispatch_attempt.provider_message_id` for one of the campaign's content
assets).

The function is intentionally a single SQL query: cheap at MVP scale and
the dashboard's "synced N min ago" indicator pulls from the same row set.
Caching / materialisation is a Phase 2 concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    and_,
    case,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import EventKind
from app.db.models import (
    AnalyticEvent,
    ContentAsset,
    DispatchAttempt,
)


# Per-provider documented ingest latency. Surface this alongside the
# observed freshness so the UI can render "synced 12 min ago (Plausible
# usually 5 min behind real-time)" honestly.
DOCUMENTED_LATENCY_SECONDS: dict[str, int] = {
    "plausible": 300,        # 5 min — Plausible's stats API freshness.
    "sendgrid": 60,          # ~1 min — event webhook fires quickly.
    "linkedin": 600,         # 10 min — stub estimate until W40 lands real data.
    "csv_upload": 0,         # we control ingest time.
    "unknown": 0,
}

@dataclass(frozen=True)
class SourceFreshness:
    """One source's freshness snapshot for the campaign."""

    name: str
    last_event_at: datetime | None
    freshness_seconds: int | None
    documented_latency_seconds: int


@dataclass(frozen=True)
class CampaignKpis:
    """Result of `compute_campaign_kpis`."""

    impressions: int = 0
    opens: int = 0
    clicks: int = 0
    replies: int = 0
    conversions: int = 0
    unsubscribes: int = 0
    bounces: int = 0
    spam_complaints: int = 0
    spend: Decimal = field(default_factory=lambda: Decimal("0"))

    # Derived metrics. Computed lazily in `as_dict` to avoid divide-by-zero.
    def as_dict(self) -> dict[str, Any]:
        denom_impr = self.impressions or 0
        denom_open = self.opens or 0
        ctr = (
            (Decimal(self.clicks) / Decimal(denom_impr)).quantize(Decimal("0.0001"))
            if denom_impr
            else Decimal("0.0000")
        )
        open_rate = (
            (Decimal(self.opens) / Decimal(denom_impr)).quantize(Decimal("0.0001"))
            if denom_impr
            else Decimal("0.0000")
        )
        click_to_open = (
            (Decimal(self.clicks) / Decimal(denom_open)).quantize(Decimal("0.0001"))
            if denom_open
            else Decimal("0.0000")
        )
        unsub_rate = (
            (Decimal(self.unsubscribes) / Decimal(denom_impr)).quantize(Decimal("0.0001"))
            if denom_impr
            else Decimal("0.0000")
        )
        cpl = (
            (self.spend / Decimal(self.conversions)).quantize(Decimal("0.01"))
            if self.conversions
            else None
        )
        cpa = cpl  # CPL == CPA at MVP — we treat conversion as the acquisition event.
        return {
            "impressions": self.impressions,
            "opens": self.opens,
            "clicks": self.clicks,
            "replies": self.replies,
            "conversions": self.conversions,
            "unsubscribes": self.unsubscribes,
            "bounces": self.bounces,
            "spam_complaints": self.spam_complaints,
            "spend": str(self.spend),
            "derived": {
                "ctr": str(ctr),
                "open_rate": str(open_rate),
                "click_to_open": str(click_to_open),
                "unsubscribe_rate": str(unsub_rate),
                "cpl": str(cpl) if cpl is not None else None,
                "cpa": str(cpa) if cpa is not None else None,
            },
        }


@dataclass(frozen=True)
class CampaignKpiSnapshot:
    """Top-level rollup with source freshness for the dashboard."""

    campaign_id: UUID
    kpis: CampaignKpis
    sources: list[SourceFreshness]
    generated_at: datetime


async def compute_campaign_kpis(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    channel_id: UUID | None = None,
    content_asset_id: UUID | None = None,
    now: datetime,
) -> CampaignKpiSnapshot:
    """Aggregate metrics + freshness for one campaign.

    Tenant scoping is enforced at the query level — even if RLS were
    bypassed, the explicit `tenant_id` filter narrows the rows.
    """
    # The set of analytic_event ids that "belong" to this campaign:
    #
    #   (a) direct attribution: analytic_event.campaign_id = ?
    #   (b) email attribution: analytic_event.payload->>'sg_message_id'
    #       matches a dispatch_attempt linked to a content_asset of this
    #       campaign.
    #
    # We express (b) as an EXISTS subquery so we don't double-count events
    # that already have direct attribution.

    direct_match = AnalyticEvent.campaign_id == campaign_id

    sg_message_id = AnalyticEvent.payload[
        "sg_message_id"
    ].astext  # JSONB ->> 'sg_message_id'
    indirect_exists = exists(
        select(DispatchAttempt.id)
        .join(
            ContentAsset,
            and_(
                ContentAsset.id == DispatchAttempt.content_asset_id,
                ContentAsset.tenant_id == tenant_id,
                ContentAsset.campaign_id == campaign_id,
            ),
        )
        .where(
            DispatchAttempt.tenant_id == tenant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    base_filter = and_(
        AnalyticEvent.tenant_id == tenant_id,
        or_(direct_match, indirect_exists),
    )

    if channel_id is not None:
        # channel_id filter: an event matches if its own channel_id matches
        # OR if its indirect-attribution content_asset's channel_id matches.
        channel_indirect = exists(
            select(DispatchAttempt.id)
            .join(
                ContentAsset,
                and_(
                    ContentAsset.id == DispatchAttempt.content_asset_id,
                    ContentAsset.tenant_id == tenant_id,
                    ContentAsset.campaign_id == campaign_id,
                    ContentAsset.channel_id == channel_id,
                ),
            )
            .where(
                DispatchAttempt.tenant_id == tenant_id,
                DispatchAttempt.provider_message_id == sg_message_id,
            )
        )
        base_filter = and_(
            base_filter,
            or_(AnalyticEvent.channel_id == channel_id, channel_indirect),
        )

    if content_asset_id is not None:
        # Content asset filter: only events whose dispatch_attempt links to
        # this asset. Plausible events have no asset link, so they're
        # excluded — which is the correct semantic (asset-scoped metrics
        # are about what we sent, not what the site recorded).
        asset_indirect = exists(
            select(DispatchAttempt.id).where(
                DispatchAttempt.tenant_id == tenant_id,
                DispatchAttempt.content_asset_id == content_asset_id,
                DispatchAttempt.provider_message_id == sg_message_id,
            )
        )
        base_filter = and_(base_filter, asset_indirect)

    # Aggregate by event_type. `spend` is summed from metric_value because
    # spend isn't a count — it's a per-event amount.
    counts_stmt = select(
        AnalyticEvent.event_type,
        func.count(AnalyticEvent.id).label("n"),
        func.coalesce(func.sum(AnalyticEvent.metric_value), 0).label("metric_sum"),
    ).where(base_filter).group_by(AnalyticEvent.event_type)
    rows = (await session.execute(counts_stmt)).all()

    by_kind: dict[EventKind, int] = {}
    spend_total = Decimal("0")
    for kind, n, metric_sum in rows:
        if kind == EventKind.spend:
            spend_total = Decimal(metric_sum or 0)
        else:
            by_kind[kind] = int(n)

    kpis = CampaignKpis(
        impressions=by_kind.get(EventKind.impression, 0),
        opens=by_kind.get(EventKind.open, 0),
        clicks=by_kind.get(EventKind.click, 0),
        replies=by_kind.get(EventKind.reply, 0),
        conversions=by_kind.get(EventKind.conversion, 0),
        unsubscribes=by_kind.get(EventKind.unsubscribe, 0),
        bounces=by_kind.get(EventKind.bounce, 0),
        spam_complaints=by_kind.get(EventKind.spam_complaint, 0),
        spend=spend_total,
    )

    sources = await _source_freshness(
        session,
        tenant_id=tenant_id,
        base_filter=base_filter,
        now=now,
    )

    return CampaignKpiSnapshot(
        campaign_id=campaign_id,
        kpis=kpis,
        sources=sources,
        generated_at=now,
    )


async def _source_freshness(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    base_filter: Any,
    now: datetime,
) -> list[SourceFreshness]:
    """Last event time per source.

    We infer "source" from the payload: SendGrid events carry
    `sg_message_id`, Plausible events carry `utm_source`/`event` semantics
    distinct from SendGrid. For MVP we use a coarse two-bucket split —
    'sendgrid' vs 'plausible' vs 'unknown' — based on payload markers.
    """
    sg_marker = AnalyticEvent.payload["sg_message_id"].astext
    source_expr = case(
        (sg_marker.isnot(None), literal("sendgrid")),
        (
            AnalyticEvent.provider_event_id.like("plausible:%"),
            literal("plausible"),
        ),
        else_=literal("unknown"),
    ).label("source")

    stmt = select(
        source_expr,
        func.max(AnalyticEvent.event_at).label("last_event_at"),
    ).where(base_filter).group_by(source_expr)
    rows = (await session.execute(stmt)).all()

    out: list[SourceFreshness] = []
    for name, last_at in rows:
        documented = DOCUMENTED_LATENCY_SECONDS.get(name, 0)
        if last_at is None:
            out.append(
                SourceFreshness(
                    name=name,
                    last_event_at=None,
                    freshness_seconds=None,
                    documented_latency_seconds=documented,
                )
            )
            continue
        freshness = int((now - last_at).total_seconds())
        out.append(
            SourceFreshness(
                name=name,
                last_event_at=last_at,
                freshness_seconds=max(freshness, 0),
                documented_latency_seconds=documented,
            )
        )
    return out


__all__ = [
    "CampaignKpiSnapshot",
    "CampaignKpis",
    "SourceFreshness",
    "compute_campaign_kpis",
    "DOCUMENTED_LATENCY_SECONDS",
]
