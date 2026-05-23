"""W37 — Optimisation recommendations (E10-S03).

Tests the budget_shift rule + duplicate suppression + the campaign-age
gate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.recommendations import (
    MIN_DATA_DAYS,
    generate_recommendations,
)
from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    EventKind,
    UserRole,
)
from app.db.models import (
    AnalyticEvent,
    AppUser,
    Campaign,
    Channel,
    ContentAsset,
    OptimisationRecommendation,
    StrategyProposal,
    Tenant,
)


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    campaign_age_days: int = 8,
    channel_a_clicks: int = 100,
    channel_a_spend: float = 100.0,
    channel_b_clicks: int = 30,
    channel_b_spend: float = 100.0,
) -> dict:
    """Two channels, one strategy proposal allocating 50/50, and N
    days of click + spend events per channel."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"rec-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@rec.test",
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
        ch_linkedin = Channel(
            tenant_id=tenant.id,
            name="LinkedIn",
            platform=ChannelPlatform.linkedin,
            is_active=True,
        )
        session.add_all([ch_email, ch_linkedin])
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("1000"),
            currency="USD",
            start_date=date.today() - timedelta(days=campaign_age_days),
            end_date=date.today() + timedelta(days=30),
            brief="b",
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        proposal = StrategyProposal(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            version=1,
            is_accepted=True,
            created_by_kind="agent",
            payload={
                "channels": [
                    {
                        "platform": "email",
                        "name": "Email",
                        "allocation_pct": 50,
                        "allocation_amount": "500.00",
                        "rationale": "x",
                        "human_override": False,
                    },
                    {
                        "platform": "linkedin",
                        "name": "LinkedIn",
                        "allocation_pct": 50,
                        "allocation_amount": "500.00",
                        "rationale": "x",
                        "human_override": False,
                    },
                ],
                "kpis": {
                    "primary": {"metric": "mql", "target": 100, "rationale": "z"},
                    "secondary": [],
                },
            },
        )
        session.add(proposal)
        await session.flush()
        now = datetime.now(UTC)
        # Channel A — Email — leader
        for i in range(channel_a_clicks):
            session.add(
                AnalyticEvent(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    channel_id=ch_email.id,
                    event_type=EventKind.click,
                    payload={},
                    event_at=now - timedelta(days=2),
                )
            )
        session.add(
            AnalyticEvent(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                channel_id=ch_email.id,
                event_type=EventKind.spend,
                metric_value=Decimal(str(channel_a_spend)),
                payload={},
                event_at=now - timedelta(days=2),
            )
        )
        # Channel B — LinkedIn — laggard
        for i in range(channel_b_clicks):
            session.add(
                AnalyticEvent(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    channel_id=ch_linkedin.id,
                    event_type=EventKind.click,
                    payload={},
                    event_at=now - timedelta(days=2),
                )
            )
        session.add(
            AnalyticEvent(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                channel_id=ch_linkedin.id,
                event_type=EventKind.spend,
                metric_value=Decimal(str(channel_b_spend)),
                payload={},
                event_at=now - timedelta(days=2),
            )
        )
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "email_channel_id": ch_email.id,
            "linkedin_channel_id": ch_linkedin.id,
        }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_budget_shift_fires_when_channels_diverge(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert len(recs) == 1
    rec = recs[0]
    assert rec.kind == "budget_shift"
    assert rec.status == "pending"
    assert rec.proposal["from"]["channel"] == "LinkedIn"
    assert rec.proposal["to"]["channel"] == "Email"
    # 20% of 50 = 10.
    assert rec.proposal["shifted_pct"] == pytest.approx(10.0)
    assert rec.predicted_uplift is not None and rec.predicted_uplift >= Decimal(0)
    assert rec.rationale and "ratio" in rec.rationale


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def test_below_min_age_returns_nothing(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine, campaign_age_days=MIN_DATA_DAYS - 2)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert recs == []


async def test_channels_too_close_does_not_fire(db_engine: AsyncEngine) -> None:
    world = await _seed_world(
        db_engine,
        channel_a_clicks=80,
        channel_b_clicks=70,  # 1.14× ratio, well below the 1.5× floor
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert recs == []


async def test_duplicate_pending_is_suppressed(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        first = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
        second = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert len(first) == 1
    assert second == []
