"""W39 — Budget rebalancing proposals (E10-S05).

Adds the W39-specific cases on top of W37's `test_recommendations.py`:
  * 5-day age gate (not 7).
  * Cost-per-outcome ratio drives the proposal; confidence label set.
  * `min_daily_spend` floor clamps or drops the shift.
  * Accept handler upserts `campaign_channel_budget` rows atomically.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.recommendations import (
    MIN_DATA_DAYS,
    generate_recommendations,
)
from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
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
    CampaignChannelBudget,
    Channel,
    OptimisationRecommendation,
    StrategyProposal,
    Tenant,
)


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    campaign_age_days: int = 6,
    a_clicks: int = 100,
    a_conversions: int = 0,
    a_spend: float = 100.0,
    b_clicks: int = 30,
    b_conversions: int = 0,
    b_spend: float = 100.0,
    b_min_daily_spend: Decimal | None = None,
    a_allocation_amount: str = "500.00",
    b_allocation_amount: str = "500.00",
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"bal-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@bal.test",
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
        b_api_config: dict = {}
        if b_min_daily_spend is not None:
            b_api_config["min_daily_spend"] = str(b_min_daily_spend)
        ch_linkedin = Channel(
            tenant_id=tenant.id,
            name="LinkedIn",
            platform=ChannelPlatform.linkedin,
            is_active=True,
            api_config=b_api_config,
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
                        "allocation_amount": a_allocation_amount,
                        "rationale": "x",
                        "human_override": False,
                    },
                    {
                        "platform": "linkedin",
                        "name": "LinkedIn",
                        "allocation_pct": 50,
                        "allocation_amount": b_allocation_amount,
                        "rationale": "x",
                        "human_override": False,
                    },
                ],
                "kpis": {
                    "primary": {"metric": "click", "target": 10, "rationale": "z"},
                    "secondary": [],
                },
            },
        )
        session.add(proposal)
        await session.flush()

        now = datetime.now(UTC)
        for ch, clicks, convs, spend_amount in (
            (ch_email, a_clicks, a_conversions, a_spend),
            (ch_linkedin, b_clicks, b_conversions, b_spend),
        ):
            for _ in range(clicks):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant.id,
                        campaign_id=campaign.id,
                        channel_id=ch.id,
                        event_type=EventKind.click,
                        payload={},
                        event_at=now - timedelta(days=2),
                    )
                )
            for _ in range(convs):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant.id,
                        campaign_id=campaign.id,
                        channel_id=ch.id,
                        event_type=EventKind.conversion,
                        payload={},
                        event_at=now - timedelta(days=2),
                    )
                )
            session.add(
                AnalyticEvent(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    channel_id=ch.id,
                    event_type=EventKind.spend,
                    metric_value=Decimal(str(spend_amount)),
                    payload={},
                    event_at=now - timedelta(days=2),
                )
            )

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "email_channel_id": ch_email.id,
            "linkedin_channel_id": ch_linkedin.id,
            "owner_id": owner.id,
        }


# ---------------------------------------------------------------------------
# Age gate (AC #1)
# ---------------------------------------------------------------------------


async def test_5_day_age_gate_unblocks_proposal(db_engine: AsyncEngine) -> None:
    """4 days → no proposal; 5 days → proposal fires (when data supports it)."""
    assert MIN_DATA_DAYS == 5

    early = await _seed_world(db_engine, campaign_age_days=4)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=early["tenant_id"],
            campaign_id=early["campaign_id"],
            now=datetime.now(UTC),
        )
    assert recs == []

    ready = await _seed_world(db_engine, campaign_age_days=5)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=ready["tenant_id"],
            campaign_id=ready["campaign_id"],
            now=datetime.now(UTC),
        )
    assert len(recs) == 1


# ---------------------------------------------------------------------------
# Confidence label (AC #2)
# ---------------------------------------------------------------------------


async def test_high_confidence_when_cpo_ratio_is_strong(
    db_engine: AsyncEngine,
) -> None:
    # leader CPO = 100/300 ≈ 0.33, laggard CPO = 100/20 = 5.0 → ratio 15× → high
    world = await _seed_world(db_engine, a_clicks=300, b_clicks=20)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert recs[0].proposal["confidence"] == "high"


async def test_medium_confidence_when_cpo_ratio_is_moderate(
    db_engine: AsyncEngine,
) -> None:
    # leader CPO = 100/180 = 0.556, laggard = 100/100 = 1.0 → ratio 1.8× → medium
    world = await _seed_world(db_engine, a_clicks=180, b_clicks=100)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert recs[0].proposal["confidence"] == "medium"


# ---------------------------------------------------------------------------
# Conversion preferred over click when present (AC #1 outcome)
# ---------------------------------------------------------------------------


async def test_conversion_preferred_when_any_present(db_engine: AsyncEngine) -> None:
    world = await _seed_world(
        db_engine,
        a_clicks=100,
        a_conversions=20,
        b_clicks=100,
        b_conversions=2,
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert len(recs) == 1
    assert recs[0].proposal["outcome"] == "conversion"


# ---------------------------------------------------------------------------
# min_daily_spend floor (AC #4)
# ---------------------------------------------------------------------------


async def test_min_daily_spend_clamps_shift(db_engine: AsyncEngine) -> None:
    """laggard's allocation is 500; min_daily_spend = 400 means the
    biggest legal shift is 100. Default 30% shift would be 150 → clamped
    to 100 (20% of 500), still above the 10% floor → proposal still
    fires with `clamped_to_floor=True`."""
    world = await _seed_world(
        db_engine,
        b_min_daily_spend=Decimal("400"),
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert len(recs) == 1
    proposal = recs[0].proposal
    assert proposal["clamped_to_floor"] is True
    assert Decimal(proposal["proposed_amount"]) == Decimal("100.00")
    assert "floor" in (recs[0].rationale or "").lower()


async def test_min_daily_spend_drops_proposal_when_shift_too_small(
    db_engine: AsyncEngine,
) -> None:
    """min_daily_spend = 480 leaves only 20 of legal shift = 4% of 500,
    below the 10% floor → no proposal."""
    world = await _seed_world(
        db_engine,
        b_min_daily_spend=Decimal("480"),
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert recs == []


# ---------------------------------------------------------------------------
# Apply handler (AC #3)
# ---------------------------------------------------------------------------


@pytest.fixture
async def world(override_api_db, db_engine: AsyncEngine) -> dict:
    return await _seed_world(db_engine)


@pytest.fixture
async def client_as(world, db_engine: AsyncEngine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            user = AppUser(
                tenant_id=world["tenant_id"],
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@bal.test",
                role=role,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
        app.dependency_overrides[get_current_user] = lambda: user
        transport = httpx.ASGITransport(app=app)
        c = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c, user

    try:
        yield _factory
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_current_user, None)


async def test_accept_upserts_campaign_channel_budget_rows(
    client_as, world, db_engine: AsyncEngine
) -> None:
    # First — generate the recommendation via the rule.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert len(recs) == 1
    rec_id = recs[0].id

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/recommendations/{rec_id}/accept")
    assert resp.status_code == 200, resp.text

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(CampaignChannelBudget).where(
                    CampaignChannelBudget.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()
    by_ch = {r.channel_id: r for r in rows}
    # Laggard (LinkedIn) drops by 30% of 500 = 150; leader (Email) gains 150.
    assert by_ch[world["linkedin_channel_id"]].allocated == Decimal("350.00")
    assert by_ch[world["email_channel_id"]].allocated == Decimal("650.00")
    # Total preserved.
    assert sum(r.allocated for r in rows) == Decimal("1000.00")


async def test_accept_idempotent_under_existing_ccb_rows(
    client_as, world, db_engine: AsyncEngine
) -> None:
    # Pre-seed CCB rows with the original allocation so the upsert path
    # is exercised in update mode.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            CampaignChannelBudget(
                campaign_id=world["campaign_id"],
                channel_id=world["email_channel_id"],
                allocated=Decimal("500.00"),
            )
        )
        session.add(
            CampaignChannelBudget(
                campaign_id=world["campaign_id"],
                channel_id=world["linkedin_channel_id"],
                allocated=Decimal("500.00"),
            )
        )
        recs = await generate_recommendations(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    rec_id = recs[0].id

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/recommendations/{rec_id}/accept")
    assert resp.status_code == 200

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        email_row = (
            await session.execute(
                select(CampaignChannelBudget).where(
                    CampaignChannelBudget.campaign_id == world["campaign_id"],
                    CampaignChannelBudget.channel_id == world["email_channel_id"],
                )
            )
        ).scalar_one()
    assert email_row.allocated == Decimal("650.00")
