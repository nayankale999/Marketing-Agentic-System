"""Dashboard stats query (W42).

One function, called once per dashboard render. Returns counts the UI
panel needs without N+1 chatter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AssetStatus, CampaignStatus
from app.db.models import (
    Campaign,
    ContentAsset,
    MetricAnomaly,
    OptimisationRecommendation,
)


@dataclass(frozen=True)
class CampaignCard:
    """One row for the 'recent campaigns' table."""

    id: UUID
    name: str
    status: str
    start_date: Any
    end_date: Any
    budget_total: Any
    currency: str
    updated_at: datetime


@dataclass(frozen=True)
class DashboardStats:
    """Counters + recent-campaigns list for the dashboard top panel."""

    total_campaigns: int
    campaigns_by_status: dict[str, int]
    pending_approvals: int
    open_anomalies: int
    critical_anomalies: int
    pending_recommendations: int
    recent_campaigns: list[CampaignCard] = field(default_factory=list)


async def load_dashboard_stats(
    session: AsyncSession,
    *,
    recent_limit: int = 6,
) -> DashboardStats:
    """Pull everything the dashboard needs in one round-trip (per query)."""
    # Campaigns by status.
    rows = (
        await session.execute(
            select(Campaign.status, func.count(Campaign.id))
            .group_by(Campaign.status)
        )
    ).all()
    by_status: dict[str, int] = {s.value: int(c) for s, c in rows}
    total = sum(by_status.values())

    # Pending approvals — content assets currently in `pending_approval`.
    pending_approvals = (
        await session.execute(
            select(func.count(ContentAsset.id)).where(
                ContentAsset.status == AssetStatus.pending_approval
            )
        )
    ).scalar_one()

    # Open anomalies (not dismissed).
    open_total = (
        await session.execute(
            select(func.count(MetricAnomaly.id)).where(
                MetricAnomaly.dismissed_at.is_(None)
            )
        )
    ).scalar_one()
    critical_open = (
        await session.execute(
            select(func.count(MetricAnomaly.id)).where(
                MetricAnomaly.dismissed_at.is_(None),
                MetricAnomaly.severity == "critical",
            )
        )
    ).scalar_one()

    pending_recs = (
        await session.execute(
            select(func.count(OptimisationRecommendation.id)).where(
                OptimisationRecommendation.status == "pending"
            )
        )
    ).scalar_one()

    # Recent campaigns — ordered by updated_at desc.
    campaign_rows = (
        await session.execute(
            select(Campaign).order_by(Campaign.updated_at.desc()).limit(recent_limit)
        )
    ).scalars().all()
    recent = [
        CampaignCard(
            id=c.id,
            name=c.name,
            status=c.status.value,
            start_date=c.start_date,
            end_date=c.end_date,
            budget_total=c.budget_total,
            currency=c.currency,
            updated_at=c.updated_at,
        )
        for c in campaign_rows
    ]

    # Ensure every CampaignStatus shows up in the dict even when 0 — keeps
    # the UI templates simple.
    full_by_status = {s.value: by_status.get(s.value, 0) for s in CampaignStatus}

    return DashboardStats(
        total_campaigns=int(total),
        campaigns_by_status=full_by_status,
        pending_approvals=int(pending_approvals),
        open_anomalies=int(open_total),
        critical_anomalies=int(critical_open),
        pending_recommendations=int(pending_recs),
        recent_campaigns=recent,
    )


__all__ = ["DashboardStats", "CampaignCard", "load_dashboard_stats"]
