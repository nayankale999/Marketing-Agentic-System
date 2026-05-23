"""W38 — End-of-campaign report generation (E10-S04).

Section coverage + versioning behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.report import generate_report
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    EventKind,
    UserRole,
)
from app.db.models import (
    AbTest,
    AnalyticEvent,
    AppUser,
    Campaign,
    CampaignReport,
    Channel,
    ContentAsset,
    MetricAnomaly,
    OptimisationRecommendation,
    Tenant,
)


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    with_events: bool = True,
    with_anomaly: bool = True,
    with_ab_test: bool = True,
    with_recommendations: bool = True,
    kpi_targets: dict | None = None,
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"rpt-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@rpt.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        ch_email = Channel(
            tenant_id=tenant.id,
            name="Email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        session.add(ch_email)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="Q3 push",
            campaign_type=CampaignType.product_launch,
            objective="Drive 100 MQLs",
            budget_total=Decimal("2500"),
            currency="USD",
            start_date=date.today() - timedelta(days=14),
            end_date=date.today(),
            brief="b",
            kpi_targets=kpi_targets
            or {
                "primary": {"metric": "click", "target": 100, "rationale": "x"},
                "secondary": [
                    {"metric": "open", "target": 500, "rationale": "y"}
                ],
            },
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()

        if with_events:
            now = datetime.now(UTC)
            for _ in range(120):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant.id,
                        campaign_id=campaign.id,
                        channel_id=ch_email.id,
                        event_type=EventKind.click,
                        payload={},
                        event_at=now - timedelta(days=1),
                    )
                )
            session.add(
                AnalyticEvent(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    channel_id=ch_email.id,
                    event_type=EventKind.spend,
                    metric_value=Decimal("300.00"),
                    payload={},
                    event_at=now - timedelta(days=1),
                )
            )

        if with_anomaly:
            session.add(
                MetricAnomaly(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    metric=EventKind.unsubscribe.value,
                    window_start=datetime.now(UTC) - timedelta(days=1),
                    window_end=datetime.now(UTC),
                    observed_value=Decimal("80"),
                    baseline_median=Decimal("3"),
                    baseline_stddev=Decimal("1"),
                    sigma=Decimal("9.0"),
                    severity="critical",
                )
            )

        if with_ab_test:
            variants = []
            for i in range(2):
                v = ContentAsset(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    asset_type=AssetType.email,
                    status=AssetStatus.published,
                    content=f"v{i}",
                )
                session.add(v)
                await session.flush()
                variants.append(v)
            ab = AbTest(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                name="Subject test",
                primary_metric="open",
                status=AbTestStatus.significant,
                variant_a_id=variants[0].id,
                variant_b_id=variants[1].id,
                winner_id=variants[1].id,
                confidence=Decimal("0.95"),
                lift=Decimal("0.18"),
            )
            session.add(ab)

        if with_recommendations:
            session.add(
                OptimisationRecommendation(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    kind="budget_shift",
                    proposal={"from": {"channel": "LinkedIn"}, "to": {"channel": "Email"}},
                    rationale="Email outperforms",
                    predicted_uplift=Decimal("0.08"),
                    status="applied",
                    applied_at=datetime.now(UTC),
                    applied_by=owner.id,
                )
            )
            session.add(
                OptimisationRecommendation(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    kind="budget_shift",
                    proposal={"from": {"channel": "Email"}, "to": {"channel": "LinkedIn"}},
                    rationale="Reverse",
                    predicted_uplift=Decimal("0.04"),
                    status="rejected",
                )
            )

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "owner_id": owner.id,
            "email_channel_id": ch_email.id,
        }


# ---------------------------------------------------------------------------
# Section coverage
# ---------------------------------------------------------------------------


async def test_report_populates_every_section(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        rpt = await generate_report(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
            generated_by="system",
        )

    d = rpt.data
    assert d["objectives"]["objective"] == "Drive 100 MQLs"
    assert d["objectives"]["budget_total"] == "2500.00"

    primary = next(k for k in d["kpis_vs_target"] if k["name"] == "click")
    assert primary["target"] == 100
    assert primary["observed"] == 120
    assert primary["delta_pct"] == 20.0

    assert any(c["name"] == "Email" for c in d["channel_breakdown"])
    assert Decimal(d["spend_total"]) == Decimal("300")

    assert d["ab_tests"][0]["status"] == "significant"
    assert d["ab_tests"][0]["lift"] == "0.1800"

    assert d["anomalies"][0]["metric"] == EventKind.unsubscribe.value
    assert d["anomalies"][0]["severity"] == "critical"

    assert d["recommendations_applied"][0]["kind"] == "budget_shift"
    assert d["recommendations_rejected"][0]["kind"] == "budget_shift"


# ---------------------------------------------------------------------------
# Null preservation (AC #3)
# ---------------------------------------------------------------------------


async def test_kpi_observed_is_null_when_no_events(db_engine: AsyncEngine) -> None:
    world = await _seed_world(
        db_engine,
        with_events=False,
        with_anomaly=False,
        with_ab_test=False,
        with_recommendations=False,
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        rpt = await generate_report(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
            generated_by="system",
        )
    primary = next(k for k in rpt.data["kpis_vs_target"] if k["name"] == "click")
    assert primary["observed"] is None
    assert primary["delta_pct"] is None
    assert rpt.data["spend_total"] is None


async def test_unknown_kpi_metric_is_skipped(db_engine: AsyncEngine) -> None:
    world = await _seed_world(
        db_engine,
        kpi_targets={
            "primary": {"metric": "made_up_metric", "target": 10},
            "secondary": [],
        },
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        rpt = await generate_report(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
            generated_by="system",
        )
    primary = next(
        (k for k in rpt.data["kpis_vs_target"] if k["name"] == "made_up_metric"),
        None,
    )
    # Unknown metrics still show up (so the marketer can see the target
    # they configured) but observed stays None.
    assert primary is not None
    assert primary["observed"] is None


# ---------------------------------------------------------------------------
# Versioning (AC #4)
# ---------------------------------------------------------------------------


async def test_regenerate_increments_version_and_flips_latest(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        first = await generate_report(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
            generated_by="system",
        )
        assert first.version == 1
        assert first.is_latest is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        second = await generate_report(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
            generated_by="system",
        )
        assert second.version == 2
        assert second.is_latest is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(CampaignReport).where(
                    CampaignReport.campaign_id == world["campaign_id"]
                ).order_by(CampaignReport.version.asc())
            )
        ).scalars().all()
    assert len(rows) == 2
    assert rows[0].is_latest is False
    assert rows[1].is_latest is True


# ---------------------------------------------------------------------------
# Auto-generate hook
# ---------------------------------------------------------------------------


async def test_complete_campaign_transition_auto_generates_report(
    db_engine: AsyncEngine,
) -> None:
    from app.db.session import set_tenant_context
    from app.orchestrator.state_machine import campaign_sm

    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await campaign_sm.apply(session, campaign, "complete_campaign")

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rpt = (
            await session.execute(
                select(CampaignReport).where(
                    CampaignReport.campaign_id == world["campaign_id"],
                    CampaignReport.is_latest.is_(True),
                )
            )
        ).scalar_one()
    assert rpt.generated_by == "system"
    assert rpt.version == 1
