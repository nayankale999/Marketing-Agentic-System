"""End-of-campaign report generation (W38, E10-S04).

Builds + persists a versioned snapshot of how a campaign performed
against its objectives. The snapshot is a single JSONB blob so the
report endpoint can return it in one read and the UI can render it
without N+1 queries.

Why a snapshot and not a live query: late-arriving events (a Plausible
backfill, a SendGrid retry) shouldn't silently rewrite a report a
stakeholder has already read. Regenerating creates a new row with
`version = N+1`; the old version stays addressable for audit (AC #4).

What lands per section
----------------------
  * objectives          — campaign brief + budget shape
  * kpis_vs_target      — each kpi_targets entry vs the observed value;
                          `null` observed when no data (AC #3)
  * channel_breakdown   — per-channel clicks / impressions / opens /
                          spend over the campaign window
  * ab_tests            — every A/B test on the campaign + winner +
                          confidence + lift
  * anomalies           — non-dismissed first, then dismissed
  * recommendations_*   — applied / rejected lists
  * spend_total         — sum(metric_value) of EventKind.spend events
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import EventKind
from app.db.models import (
    AbTest,
    AnalyticEvent,
    Campaign,
    CampaignReport,
    Channel,
    ContentAsset,
    DispatchAttempt,
    MetricAnomaly,
    OptimisationRecommendation,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def generate_report(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    now: datetime,
    generated_by: str,
) -> CampaignReport:
    """Build a fresh report row, marking it `is_latest`. Bumps any prior
    latest row down to `is_latest=false` first so the partial unique
    index is happy."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise LookupError(f"campaign {campaign_id} not found")

    data = await _build_data(session, tenant_id=tenant_id, campaign=campaign)

    # Demote the prior latest before inserting the new one. SQLAlchemy
    # auto-commits the UPDATE before the INSERT inside the same outer
    # transaction; the partial unique index permits the transient state
    # as long as the surrounding transaction is consistent.
    prior_latest = (
        await session.execute(
            select(CampaignReport).where(
                CampaignReport.tenant_id == tenant_id,
                CampaignReport.campaign_id == campaign_id,
                CampaignReport.is_latest.is_(True),
            )
        )
    ).scalar_one_or_none()
    next_version = 1
    if prior_latest is not None:
        prior_latest.is_latest = False
        await session.flush()
        next_version = prior_latest.version + 1
    else:
        # Could be a regeneration after a previous demotion (rare). Pick
        # the highest existing version regardless.
        existing_max = (
            await session.execute(
                select(func.max(CampaignReport.version)).where(
                    CampaignReport.tenant_id == tenant_id,
                    CampaignReport.campaign_id == campaign_id,
                )
            )
        ).scalar_one()
        if existing_max is not None:
            next_version = int(existing_max) + 1

    row = CampaignReport(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        version=next_version,
        generated_at=now,
        generated_by=generated_by,
        data=data,
        is_latest=True,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


async def _build_data(
    session: AsyncSession, *, tenant_id: UUID, campaign: Campaign
) -> dict[str, Any]:
    return {
        "objectives": _objectives_section(campaign),
        "kpis_vs_target": await _kpis_vs_target(
            session, tenant_id=tenant_id, campaign=campaign
        ),
        "channel_breakdown": await _channel_breakdown(
            session, tenant_id=tenant_id, campaign_id=campaign.id
        ),
        "ab_tests": await _ab_tests_section(
            session, tenant_id=tenant_id, campaign_id=campaign.id
        ),
        "anomalies": await _anomalies_section(
            session, tenant_id=tenant_id, campaign_id=campaign.id
        ),
        "recommendations_applied": await _recommendations_by_status(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign.id,
            status="applied",
        ),
        "recommendations_rejected": await _recommendations_by_status(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign.id,
            status="rejected",
        ),
        "spend_total": await _spend_total(
            session, tenant_id=tenant_id, campaign_id=campaign.id
        ),
    }


def _objectives_section(campaign: Campaign) -> dict[str, Any]:
    return {
        "name": campaign.name,
        "objective": campaign.objective,
        "brief": campaign.brief,
        "budget_total": str(campaign.budget_total),
        "currency": campaign.currency,
        "start_date": campaign.start_date.isoformat(),
        "end_date": campaign.end_date.isoformat(),
        "status": campaign.status.value,
    }


async def _kpis_vs_target(
    session: AsyncSession, *, tenant_id: UUID, campaign: Campaign
) -> list[dict[str, Any]]:
    """One row per entry in `campaign.kpi_targets`.

    `observed` is `null` when no analytic_event of the relevant kind has
    landed — AC #3 of E10-S04: data absence ≠ zero. Targets without a
    `metric` key are skipped; we don't invent a count.
    """
    targets_blob = campaign.kpi_targets or {}
    # kpi_targets shape isn't strictly defined; the audience-targeting and
    # strategist payloads use {primary: {metric, target, ...}, secondary: [...]}.
    targets: list[dict[str, Any]] = []
    if isinstance(targets_blob, dict):
        primary = targets_blob.get("primary")
        if isinstance(primary, dict):
            targets.append(primary)
        secondary = targets_blob.get("secondary") or []
        if isinstance(secondary, list):
            targets.extend(t for t in secondary if isinstance(t, dict))

    out: list[dict[str, Any]] = []
    for t in targets:
        metric = t.get("metric")
        if not metric:
            continue
        target = t.get("target")
        observed = await _count_metric(
            session, tenant_id=tenant_id, campaign_id=campaign.id, metric=metric
        )
        # AC #3: preserve absence — return None (JSON null) rather than 0
        # when no events have been recorded at all.
        delta_pct: float | None = None
        if isinstance(target, (int, float)) and target > 0 and observed is not None:
            delta_pct = round(((observed - target) / target) * 100, 2)

        out.append(
            {
                "name": metric,
                "target": target,
                "observed": observed,
                "delta_pct": delta_pct,
                "rationale": t.get("rationale"),
            }
        )
    return out


async def _count_metric(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    metric: str,
) -> int | None:
    """Return the raw count of analytic_events of `metric` for the
    campaign — `None` when the metric isn't an EventKind we recognise.
    """
    try:
        event_kind = EventKind(metric)
    except ValueError:
        return None

    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    indirect_link = exists(
        select(DispatchAttempt.id)
        .join(
            ContentAsset,
            (ContentAsset.id == DispatchAttempt.content_asset_id)
            & (ContentAsset.campaign_id == campaign_id),
        )
        .where(
            DispatchAttempt.tenant_id == tenant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    base_filter = (
        (AnalyticEvent.tenant_id == tenant_id)
        & (AnalyticEvent.event_type == event_kind)
        & ((AnalyticEvent.campaign_id == campaign_id) | indirect_link)
    )

    # Distinguish "no data ever" (return None) from "0 events recorded"
    # (return 0). If the campaign has zero events of ANY kind in
    # analytic_event, treat metric counts as None.
    any_events = (
        await session.execute(
            select(func.count())
            .select_from(AnalyticEvent)
            .where(
                AnalyticEvent.tenant_id == tenant_id,
                (AnalyticEvent.campaign_id == campaign_id) | indirect_link,
            )
            .limit(1)
        )
    ).scalar_one()
    if any_events == 0:
        return None

    count = (
        await session.execute(
            select(func.count(AnalyticEvent.id)).where(base_filter)
        )
    ).scalar_one()
    return int(count)


async def _channel_breakdown(
    session: AsyncSession, *, tenant_id: UUID, campaign_id: UUID
) -> list[dict[str, Any]]:
    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    indirect_link = exists(
        select(DispatchAttempt.id)
        .join(
            ContentAsset,
            (ContentAsset.id == DispatchAttempt.content_asset_id)
            & (ContentAsset.campaign_id == campaign_id),
        )
        .where(
            DispatchAttempt.tenant_id == tenant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    stmt = (
        select(
            AnalyticEvent.channel_id,
            AnalyticEvent.event_type,
            func.count(AnalyticEvent.id),
            func.coalesce(func.sum(AnalyticEvent.metric_value), 0),
        )
        .where(
            AnalyticEvent.tenant_id == tenant_id,
            (AnalyticEvent.campaign_id == campaign_id) | indirect_link,
        )
        .group_by(AnalyticEvent.channel_id, AnalyticEvent.event_type)
    )
    rows = (await session.execute(stmt)).all()

    by_channel: dict[UUID | None, dict[str, Any]] = {}
    for channel_id, event_type, count, metric_sum in rows:
        bucket = by_channel.setdefault(
            channel_id,
            {
                "clicks": 0,
                "impressions": 0,
                "opens": 0,
                "conversions": 0,
                "spend": "0",
            },
        )
        if event_type == EventKind.click:
            bucket["clicks"] += int(count)
        elif event_type == EventKind.impression:
            bucket["impressions"] += int(count)
        elif event_type == EventKind.open:
            bucket["opens"] += int(count)
        elif event_type == EventKind.conversion:
            bucket["conversions"] += int(count)
        elif event_type == EventKind.spend:
            bucket["spend"] = str(Decimal(metric_sum or 0))

    # Resolve channel names lazily; the result set is small.
    out: list[dict[str, Any]] = []
    for channel_id, bucket in by_channel.items():
        name = "(unattributed)"
        if channel_id is not None:
            name = (
                await session.execute(
                    select(Channel.name).where(Channel.id == channel_id)
                )
            ).scalar_one_or_none() or str(channel_id)
        out.append(
            {
                "channel_id": str(channel_id) if channel_id else None,
                "name": name,
                **bucket,
            }
        )
    out.sort(key=lambda row: row["name"])
    return out


async def _ab_tests_section(
    session: AsyncSession, *, tenant_id: UUID, campaign_id: UUID
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(AbTest)
            .where(
                AbTest.tenant_id == tenant_id,
                AbTest.campaign_id == campaign_id,
            )
            .order_by(AbTest.created_at.asc())
        )
    ).scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "primary_metric": t.primary_metric,
            "status": t.status.value,
            "winner_id": str(t.winner_id) if t.winner_id else None,
            "confidence": str(t.confidence) if t.confidence is not None else None,
            "lift": str(t.lift) if t.lift is not None else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "stopped_at": t.stopped_at.isoformat() if t.stopped_at else None,
        }
        for t in rows
    ]


async def _anomalies_section(
    session: AsyncSession, *, tenant_id: UUID, campaign_id: UUID
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(MetricAnomaly)
            .where(
                MetricAnomaly.tenant_id == tenant_id,
                MetricAnomaly.campaign_id == campaign_id,
            )
            .order_by(
                MetricAnomaly.dismissed_at.asc().nullsfirst(),
                MetricAnomaly.created_at.desc(),
            )
        )
    ).scalars().all()
    return [
        {
            "id": str(a.id),
            "metric": a.metric,
            "severity": a.severity,
            "sigma": str(a.sigma),
            "observed_value": str(a.observed_value),
            "baseline_median": str(a.baseline_median),
            "dismissed_at": a.dismissed_at.isoformat() if a.dismissed_at else None,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


async def _recommendations_by_status(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    status: str,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(OptimisationRecommendation).where(
                OptimisationRecommendation.tenant_id == tenant_id,
                OptimisationRecommendation.campaign_id == campaign_id,
                OptimisationRecommendation.status == status,
            )
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "kind": r.kind,
            "proposal": r.proposal,
            "rationale": r.rationale,
            "predicted_uplift": str(r.predicted_uplift)
            if r.predicted_uplift is not None
            else None,
            "applied_at": r.applied_at.isoformat() if r.applied_at else None,
            "applied_by": str(r.applied_by) if r.applied_by else None,
        }
        for r in rows
    ]


async def _spend_total(
    session: AsyncSession, *, tenant_id: UUID, campaign_id: UUID
) -> str | None:
    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    indirect_link = exists(
        select(DispatchAttempt.id)
        .join(
            ContentAsset,
            (ContentAsset.id == DispatchAttempt.content_asset_id)
            & (ContentAsset.campaign_id == campaign_id),
        )
        .where(
            DispatchAttempt.tenant_id == tenant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    # Same null-vs-zero distinction as KPIs: if there are no events at
    # all, return null (data absence). If there are events but no spend
    # rows, return "0".
    any_events = (
        await session.execute(
            select(func.count())
            .select_from(AnalyticEvent)
            .where(
                AnalyticEvent.tenant_id == tenant_id,
                (AnalyticEvent.campaign_id == campaign_id) | indirect_link,
            )
            .limit(1)
        )
    ).scalar_one()
    if any_events == 0:
        return None

    total = (
        await session.execute(
            select(func.coalesce(func.sum(AnalyticEvent.metric_value), 0)).where(
                AnalyticEvent.tenant_id == tenant_id,
                AnalyticEvent.event_type == EventKind.spend,
                (AnalyticEvent.campaign_id == campaign_id) | indirect_link,
            )
        )
    ).scalar_one()
    return str(Decimal(total or 0))


__all__ = ["generate_report"]
