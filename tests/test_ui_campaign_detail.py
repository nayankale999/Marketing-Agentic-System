"""W32 — Campaign detail UI (E13-S02).

Smoke + content checks against the rendered HTML. Markup is shaped to be
robust to small CSS tweaks: we assert section anchors + key data points
rather than exact DOM."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AppUser,
    Audience,
    Campaign,
    Channel,
    ContentAsset,
    StrategyProposal,
    StrategyTouchpoint,
    Tenant,
)


async def _seed_world(db_engine: AsyncEngine) -> dict[str, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ui-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        session.add(
            Channel(
                tenant_id=tenant.id,
                name="Email",
                platform=ChannelPlatform.email,
                is_active=True,
            )
        )
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@ui.test",
            display_name="Pat Marketing",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()

        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="Q3 product launch",
            campaign_type=CampaignType.product_launch,
            objective="Acquire 500 MQLs",
            brief="Beta launch of the new SMB tier targeting EMEA software buyers.",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=CampaignStatus.live,
            kpi_targets={"primary": "mql_count", "target": 500},
        )
        session.add(campaign)
        await session.flush()

        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="EMEA SMBs",
            segment_criteria={"region": "EMEA", "size": "SMB"},
            estimated_size=1200,
            actual_size=1200,
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()

        proposal = StrategyProposal(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            version=1,
            payload={
                "channels": [
                    {
                        "platform": "email",
                        "name": "Email",
                        "allocation_pct": 60,
                        "allocation_amount": "6000.00",
                        "rationale": "Direct line to known leads",
                        "human_override": False,
                    },
                ],
                "kpis": {
                    "primary": {"metric": "mql_count", "target": 500, "rationale": "stated objective"},
                    "secondary": [],
                },
            },
            is_accepted=True,
            created_by_kind="agent",
        )
        session.add(proposal)
        await session.flush()

        tp = StrategyTouchpoint(
            tenant_id=tenant.id,
            proposal_id=proposal.id,
            channel_platform="email",
            audience_id=audience.id,
            scheduled_at=datetime.combine(date(2026, 6, 7), time(9, 0), UTC),
            position=1,
        )
        session.add(tp)
        await session.flush()

        asset = ContentAsset(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            asset_type=AssetType.email,
            status=AssetStatus.pending_approval,
            title="Welcome to the SMB tier",
            content="Hi {{first_name}}, here's an early look.",
            extra_metadata={"channel_platform": "email", "fields": {"subject": "Welcome to the SMB tier"}},
            is_required=True,
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(asset)
        await session.flush()

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "asset_id": asset.id,
        }


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@ui.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def world(override_api_db, db_engine: AsyncEngine):
    return await _seed_world(db_engine)


@pytest.fixture
async def client_as(world, db_engine: AsyncEngine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, world["tenant_id"], role)
        app.dependency_overrides[get_current_user] = lambda: user
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client, user

    try:
        yield _factory
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_current_user, None)


async def test_campaign_detail_renders_with_seeded_data(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/ui/campaigns/{world['campaign_id']}")
    assert resp.status_code == 200
    body = resp.text

    # Header
    assert "Q3 product launch" in body
    assert "live" in body  # status badge

    # Brief
    assert 'id="brief"' in body
    assert "Acquire 500 MQLs" in body
    assert "Beta launch of the new SMB tier" in body

    # Audience
    assert 'id="audience"' in body
    assert "EMEA SMBs" in body
    assert "1200" in body  # member count

    # Strategy
    assert 'id="strategy"' in body
    assert "email" in body
    assert "60" in body  # allocation %

    # Content
    assert 'id="content"' in body
    assert "Welcome to the SMB tier" in body

    # Schedule
    assert 'id="schedule"' in body

    # Placeholders for Runs + Reports
    assert "W34" in body  # Runs placeholder mentions W34
    assert "W38" in body  # Reports placeholder mentions W38


async def test_campaign_detail_404_for_unknown_id(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/ui/campaigns/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_campaign_detail_review_link_appears_for_pending_assets(
    client_as, world
) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/ui/campaigns/{world['campaign_id']}")
    assert resp.status_code == 200
    assert f"/ui/approvals/{world['asset_id']}" in resp.text


async def test_campaign_detail_renders_without_audience_or_strategy(
    db_engine: AsyncEngine, override_api_db
) -> None:
    """Bare campaign with no audience / proposal / assets still renders
    the page, just with empty-state messages per section."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"bare-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name="Bare campaign",
            campaign_type=CampaignType.awareness,
            objective="x",
            brief=None,
            budget_total=Decimal("100.00"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=7),
            status=CampaignStatus.drafted,
        )
        session.add(campaign)
        await session.flush()
        tenant_id = tenant.id
        campaign_id = campaign.id

    user = await _make_user(db_engine, tenant_id, UserRole.viewer)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/ui/campaigns/{campaign_id}")
            assert resp.status_code == 200
            body = resp.text
            assert "Bare campaign" in body
            assert "No audience materialised" in body
            assert "No strategy proposal" in body
            assert "No content drafted" in body
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_static_css_serves(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get("/static/css/app.css")
    assert resp.status_code == 200
    assert "MAS minimal UI" in resp.text  # the header comment
