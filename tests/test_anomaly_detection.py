"""W37 — Anomaly detection (E10-S02).

Covers the detector + auto-pause helper + dismiss flow. We seed the
analytic_event series synthetically so the math is deterministic and the
tests are fast.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.anomaly import (
    BASELINE_DAYS,
    detect_anomalies,
    dismiss_anomaly,
    should_auto_pause,
)
from app.db.enums import (
    CampaignStatus,
    CampaignType,
    EventKind,
    UserRole,
)
from app.db.models import (
    AnalyticEvent,
    AppUser,
    AuditLog,
    Campaign,
    MetricAnomaly,
    Tenant,
    TenantComplianceSettings,
)


async def _seed_campaign(db_engine: AsyncEngine) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"anom-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@anom.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("0"),
            currency="USD",
            start_date=date.today() - timedelta(days=20),
            end_date=date.today() + timedelta(days=10),
            brief="b",
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "owner_id": owner.id,
        }


async def _seed_daily_counts(
    db_engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    metric: EventKind,
    counts: list[int],
    now: datetime,
) -> None:
    """Insert `counts[i]` events on day (now - (len-1-i)). Latest is counts[-1]."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        n = len(counts)
        for i, count in enumerate(counts):
            day = now - timedelta(days=(n - 1 - i))
            for _ in range(count):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant_id,
                        campaign_id=campaign_id,
                        event_type=metric,
                        payload={},
                        provider_event_id=f"seed-{uuid.uuid4().hex[:10]}",
                        event_at=day,
                    )
                )


# ---------------------------------------------------------------------------
# 3σ threshold
# ---------------------------------------------------------------------------


async def test_3sigma_spike_fires_anomaly(db_engine: AsyncEngine) -> None:
    world = await _seed_campaign(db_engine)
    now = datetime.now(UTC)
    # 13 days of stable counts (median 5, low stddev) then one huge spike.
    counts = [5] * 13 + [120]
    await _seed_daily_counts(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        metric=EventKind.unsubscribe,
        counts=counts,
        now=now,
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        anomalies = await detect_anomalies(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.metric == EventKind.unsubscribe.value
    assert a.severity == "critical"
    assert a.observed_value == Decimal("120")
    assert a.sigma > Decimal("3.0")


async def test_below_threshold_does_not_fire(db_engine: AsyncEngine) -> None:
    world = await _seed_campaign(db_engine)
    now = datetime.now(UTC)
    counts = [5, 6, 5, 7, 6, 5, 4, 6, 5, 7, 6, 5, 6, 7]
    await _seed_daily_counts(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        metric=EventKind.open,
        counts=counts,
        now=now,
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        anomalies = await detect_anomalies(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert anomalies == []


async def test_warning_severity_for_non_critical_metric(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_campaign(db_engine)
    now = datetime.now(UTC)
    await _seed_daily_counts(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        metric=EventKind.click,
        counts=[8] * 13 + [200],
        now=now,
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        anomalies = await detect_anomalies(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert len(anomalies) == 1
    assert anomalies[0].severity == "warning"


async def test_insufficient_baseline_skips_detection(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_campaign(db_engine)
    now = datetime.now(UTC)
    # Only 5 days of data — below BASELINE_DAYS.
    await _seed_daily_counts(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        metric=EventKind.unsubscribe,
        counts=[5] * 4 + [500],
        now=now,
    )
    assert BASELINE_DAYS == 14  # sanity
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        anomalies = await detect_anomalies(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert anomalies == []


# ---------------------------------------------------------------------------
# Notification + dismiss
# ---------------------------------------------------------------------------


async def test_critical_anomaly_writes_notification_audit(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_campaign(db_engine)
    now = datetime.now(UTC)
    await _seed_daily_counts(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        metric=EventKind.bounce,
        counts=[2] * 13 + [50],
        now=now,
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        anomalies = await detect_anomalies(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert len(anomalies) == 1

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "metric_anomaly",
                    AuditLog.entity_id == anomalies[0].id,
                    AuditLog.action == "anomaly_notification_dispatched",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        meta = audits[0].extra_metadata
        assert meta["metric"] == EventKind.bounce.value
        assert meta["severity"] == "critical"
        assert str(world["owner_id"]) in meta["recipients"]


async def test_dismissed_anomaly_is_silenced_for_24h(db_engine: AsyncEngine) -> None:
    world = await _seed_campaign(db_engine)
    now = datetime.now(UTC)
    await _seed_daily_counts(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        metric=EventKind.unsubscribe,
        counts=[5] * 13 + [200],
        now=now,
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        first = await detect_anomalies(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert len(first) == 1

    # A second detection pass within the window should NOT create another.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        second = await detect_anomalies(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now + timedelta(minutes=10),
        )
    assert second == []


# ---------------------------------------------------------------------------
# Auto-pause
# ---------------------------------------------------------------------------


async def test_auto_pause_off_by_default(db_engine: AsyncEngine) -> None:
    world = await _seed_campaign(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        ok = await should_auto_pause(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
        )
    assert ok is False


async def test_auto_pause_fires_after_two_consecutive_critical(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_campaign(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            TenantComplianceSettings(
                tenant_id=world["tenant_id"],
                auto_pause_on_critical_anomaly=True,
            )
        )
        # Two undismissed critical anomalies on the same metric.
        for i in range(2):
            session.add(
                MetricAnomaly(
                    tenant_id=world["tenant_id"],
                    campaign_id=world["campaign_id"],
                    metric=EventKind.bounce.value,
                    window_start=datetime.now(UTC) - timedelta(days=i + 1),
                    window_end=datetime.now(UTC) - timedelta(days=i),
                    observed_value=Decimal("50"),
                    baseline_median=Decimal("2"),
                    baseline_stddev=Decimal("1"),
                    sigma=Decimal("9.0"),
                    severity="critical",
                )
            )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        ok = await should_auto_pause(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
        )
    assert ok is True


async def test_dismiss_sets_dismissed_columns(db_engine: AsyncEngine) -> None:
    world = await _seed_campaign(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        admin = AppUser(
            tenant_id=world["tenant_id"],
            email=f"a-{uuid.uuid4().hex[:6]}@anom.test",
            role=UserRole.admin,
            is_active=True,
        )
        session.add(admin)
        anomaly = MetricAnomaly(
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            metric=EventKind.unsubscribe.value,
            window_start=datetime.now(UTC) - timedelta(days=1),
            window_end=datetime.now(UTC),
            observed_value=Decimal("100"),
            baseline_median=Decimal("3"),
            baseline_stddev=Decimal("1"),
            sigma=Decimal("9.0"),
            severity="critical",
        )
        session.add(anomaly)
        await session.flush()
        admin_id = admin.id
        anomaly_id = anomaly.id

    now = datetime.now(UTC)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        row = await dismiss_anomaly(
            session, anomaly_id=anomaly_id, dismissed_by=admin_id, now=now
        )
    assert row.dismissed_at == now
    assert row.dismissed_by == admin_id
