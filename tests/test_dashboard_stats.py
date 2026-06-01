"""W42 — Dashboard stats query.

Exercises the count aggregations on a populated tenant. Cheap; no
LLM involved."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.dashboard.stats import load_dashboard_stats
from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    EventKind,
    UserRole,
)
from app.db.models import (
    AppUser,
    Campaign,
    ContentAsset,
    MetricAnomaly,
    OptimisationRecommendation,
    Tenant,
)


async def _seed_world(db_engine: AsyncEngine) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ds-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@ds.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        # 3 campaigns: 1 live, 1 paused, 1 completed.
        campaigns: list[Campaign] = []
        for status in (CampaignStatus.live, CampaignStatus.paused, CampaignStatus.completed):
            c = Campaign(
                tenant_id=tenant.id,
                owner_id=owner.id,
                name=f"camp-{status.value}",
                campaign_type=CampaignType.product_launch,
                objective="o",
                budget_total=Decimal("1000"),
                currency="USD",
                start_date=date.today() - timedelta(days=10),
                end_date=date.today() + timedelta(days=10),
                brief="b",
                status=status,
            )
            session.add(c)
            await session.flush()
            campaigns.append(c)

        # Two pending approvals.
        for _ in range(2):
            session.add(
                ContentAsset(
                    tenant_id=tenant.id,
                    campaign_id=campaigns[0].id,
                    asset_type=AssetType.email,
                    status=AssetStatus.pending_approval,
                    content="x",
                )
            )

        # Two anomalies — one critical, one warning. Both undismissed.
        session.add(
            MetricAnomaly(
                tenant_id=tenant.id,
                campaign_id=campaigns[0].id,
                metric=EventKind.unsubscribe.value,
                window_start=datetime.now(UTC) - timedelta(days=1),
                window_end=datetime.now(UTC),
                observed_value=Decimal("50"),
                baseline_median=Decimal("2"),
                baseline_stddev=Decimal("1"),
                sigma=Decimal("9.0"),
                severity="critical",
            )
        )
        session.add(
            MetricAnomaly(
                tenant_id=tenant.id,
                campaign_id=campaigns[0].id,
                metric=EventKind.click.value,
                window_start=datetime.now(UTC) - timedelta(days=1),
                window_end=datetime.now(UTC),
                observed_value=Decimal("8"),
                baseline_median=Decimal("100"),
                baseline_stddev=Decimal("10"),
                sigma=Decimal("9.0"),
                severity="warning",
            )
        )

        # Dismissed anomaly — should not count.
        session.add(
            MetricAnomaly(
                tenant_id=tenant.id,
                campaign_id=campaigns[0].id,
                metric=EventKind.bounce.value,
                window_start=datetime.now(UTC) - timedelta(days=2),
                window_end=datetime.now(UTC) - timedelta(days=1),
                observed_value=Decimal("3"),
                baseline_median=Decimal("1"),
                baseline_stddev=Decimal("0.5"),
                sigma=Decimal("4.0"),
                severity="critical",
                dismissed_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )

        # 1 pending recommendation, 1 applied (should not count).
        for status in ("pending", "applied"):
            session.add(
                OptimisationRecommendation(
                    tenant_id=tenant.id,
                    campaign_id=campaigns[0].id,
                    kind="budget_shift",
                    proposal={},
                    rationale="r",
                    predicted_uplift=Decimal("0.10"),
                    status=status,
                )
            )

        return {"tenant_id": tenant.id, "campaign_ids": [c.id for c in campaigns]}


async def test_stats_counts(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        stats = await load_dashboard_stats(session)
    assert stats.total_campaigns >= 3
    # `live`, `paused`, `completed` populated.
    assert stats.campaigns_by_status["live"] >= 1
    assert stats.campaigns_by_status["paused"] >= 1
    assert stats.campaigns_by_status["completed"] >= 1
    assert stats.pending_approvals >= 2
    assert stats.open_anomalies >= 2  # dismissed excluded
    assert stats.critical_anomalies >= 1
    assert stats.pending_recommendations >= 1


async def test_recent_campaigns_returned_in_order(db_engine: AsyncEngine) -> None:
    await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        stats = await load_dashboard_stats(session, recent_limit=3)
    assert len(stats.recent_campaigns) <= 3
    # Descending by updated_at.
    times = [c.updated_at for c in stats.recent_campaigns]
    assert times == sorted(times, reverse=True)


async def test_every_status_present_in_breakdown(db_engine: AsyncEngine) -> None:
    """The breakdown dict must include every CampaignStatus so the
    template can iterate without KeyError."""
    await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        stats = await load_dashboard_stats(session)
    for s in CampaignStatus:
        assert s.value in stats.campaigns_by_status
