"""W34 — Campaign KPI rollup + dashboard endpoint (E10-S01).

Coverage:
  * Empty campaign → zeros, no divide-by-zero in derived metrics.
  * Direct-attribution events (Plausible-style: campaign_id pre-set)
    + indirect-attribution events (SendGrid: stitched via
    dispatch_attempt.provider_message_id) roll up together.
  * channel_id filter narrows to that channel for both attribution paths.
  * content_asset_id filter narrows to that asset (Plausible excluded by
    design — asset-scope is what we sent, not what the site saw).
  * Cross-tenant isolation — another tenant's events stay hidden.
  * Source freshness reflects the most recent event per source.
  * Endpoint requires viewer role + tenant-owned campaign.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.kpi_rollup import compute_campaign_kpis
from app.api.app import app
from app.api.deps import get_current_user
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
    DispatchAttempt,
    Tenant,
)


async def _seed_world(
    db_engine: AsyncEngine,
) -> dict:
    """One tenant, one campaign, one email channel, one content asset.

    The asset is what dispatch_attempt rows hang off; the email channel is
    what we filter on. We return ids so the test can build events that
    reference them.
    """
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"kpi-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()

        channel = Channel(
            tenant_id=tenant.id,
            name="Lifecycle email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        social_channel = Channel(
            tenant_id=tenant.id,
            name="LinkedIn",
            platform=ChannelPlatform.linkedin,
            is_active=True,
        )
        session.add_all([channel, social_channel])
        await session.flush()

        campaign = Campaign(
            tenant_id=tenant.id,
            name="Q3 launch",
            campaign_type=CampaignType.product_launch,
            objective="ship",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            brief="x",
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()

        asset = ContentAsset(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            channel_id=channel.id,
            asset_type=AssetType.email,
            status=AssetStatus.scheduled,
            content="hello",
        )
        social_asset = ContentAsset(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            channel_id=social_channel.id,
            asset_type=AssetType.social_post,
            status=AssetStatus.scheduled,
            content="hi linkedin",
        )
        session.add_all([asset, social_asset])
        await session.flush()

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "email_channel_id": channel.id,
            "linkedin_channel_id": social_channel.id,
            "asset_id": asset.id,
            "social_asset_id": social_asset.id,
        }


async def _insert_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: EventKind,
    campaign_id: uuid.UUID | None = None,
    channel_id: uuid.UUID | None = None,
    sg_message_id: str | None = None,
    plausible: bool = False,
    metric_value: Decimal | None = None,
    event_at: datetime | None = None,
) -> uuid.UUID:
    payload: dict = {}
    provider_event_id: str | None = None
    if sg_message_id is not None:
        payload["sg_message_id"] = sg_message_id
        provider_event_id = f"sg-{uuid.uuid4().hex[:8]}"
    if plausible:
        provider_event_id = f"plausible:site:2026-05-01:utm-1:{uuid.uuid4().hex[:8]}"
    row = AnalyticEvent(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        channel_id=channel_id,
        event_type=event_type,
        metric_value=metric_value,
        payload=payload,
        provider_event_id=provider_event_id,
        event_at=event_at or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row.id


async def _insert_dispatch_attempt(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    content_asset_id: uuid.UUID,
    provider_message_id: str,
    provider: str = "sendgrid",
) -> uuid.UUID:
    row = DispatchAttempt(
        tenant_id=tenant_id,
        content_asset_id=content_asset_id,
        recipient_identifier=f"u-{uuid.uuid4().hex[:6]}@cust.com",
        idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        provider=provider,
        provider_message_id=provider_message_id,
        status="sent",
    )
    session.add(row)
    await session.flush()
    return row.id


# ---------------------------------------------------------------------------
# Rollup function — empty case
# ---------------------------------------------------------------------------


async def test_empty_campaign_returns_zeros(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    now = datetime.now(UTC)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        snap = await compute_campaign_kpis(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert snap.kpis.impressions == 0
    assert snap.kpis.spend == Decimal("0")
    derived = snap.kpis.as_dict()["derived"]
    # No divide-by-zero — derived metrics are "0.0000" / None.
    assert derived["ctr"] == "0.0000"
    assert derived["cpl"] is None
    assert snap.sources == []


# ---------------------------------------------------------------------------
# Direct + indirect attribution roll up together
# ---------------------------------------------------------------------------


async def test_direct_and_indirect_attribution_combine(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    now = datetime.now(UTC)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # Plausible-style (direct): campaign_id set.
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            event_type=EventKind.impression,
            plausible=True,
            metric_value=Decimal("12"),
        )
        # SendGrid-style (indirect): no campaign_id, but sg_message_id links
        # via dispatch_attempt to this campaign's content_asset.
        sg_msg = "msg-abc"
        await _insert_dispatch_attempt(
            session,
            tenant_id=world["tenant_id"],
            content_asset_id=world["asset_id"],
            provider_message_id=sg_msg,
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            event_type=EventKind.open,
            sg_message_id=sg_msg,
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            event_type=EventKind.click,
            sg_message_id=sg_msg,
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            event_type=EventKind.spend,
            campaign_id=world["campaign_id"],
            metric_value=Decimal("42.50"),
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        snap = await compute_campaign_kpis(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )

    assert snap.kpis.impressions == 1
    assert snap.kpis.opens == 1
    assert snap.kpis.clicks == 1
    assert snap.kpis.spend == Decimal("42.50")
    # Sources reflect both providers.
    names = {s.name for s in snap.sources}
    assert "sendgrid" in names
    assert "plausible" in names


# ---------------------------------------------------------------------------
# Channel filter narrows both attribution paths
# ---------------------------------------------------------------------------


async def test_channel_filter_includes_indirect_via_asset(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    now = datetime.now(UTC)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # Email asset gets an open.
        await _insert_dispatch_attempt(
            session,
            tenant_id=world["tenant_id"],
            content_asset_id=world["asset_id"],
            provider_message_id="msg-email",
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            event_type=EventKind.open,
            sg_message_id="msg-email",
        )
        # LinkedIn asset gets an impression (direct, channel_id=linkedin).
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            channel_id=world["linkedin_channel_id"],
            event_type=EventKind.impression,
            plausible=True,
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        email_only = await compute_campaign_kpis(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            channel_id=world["email_channel_id"],
            now=now,
        )
        linkedin_only = await compute_campaign_kpis(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            channel_id=world["linkedin_channel_id"],
            now=now,
        )
    assert email_only.kpis.opens == 1
    assert email_only.kpis.impressions == 0
    assert linkedin_only.kpis.opens == 0
    assert linkedin_only.kpis.impressions == 1


# ---------------------------------------------------------------------------
# Content-asset filter
# ---------------------------------------------------------------------------


async def test_content_asset_filter_restricts_to_that_asset(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    now = datetime.now(UTC)
    other_asset_id: uuid.UUID
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        other_asset = ContentAsset(
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            channel_id=world["email_channel_id"],
            asset_type=AssetType.email,
            status=AssetStatus.scheduled,
            content="other",
        )
        session.add(other_asset)
        await session.flush()
        other_asset_id = other_asset.id

        await _insert_dispatch_attempt(
            session,
            tenant_id=world["tenant_id"],
            content_asset_id=world["asset_id"],
            provider_message_id="msg-A",
        )
        await _insert_dispatch_attempt(
            session,
            tenant_id=world["tenant_id"],
            content_asset_id=other_asset_id,
            provider_message_id="msg-B",
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            event_type=EventKind.open,
            sg_message_id="msg-A",
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            event_type=EventKind.open,
            sg_message_id="msg-B",
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        only_a = await compute_campaign_kpis(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            content_asset_id=world["asset_id"],
            now=now,
        )
    assert only_a.kpis.opens == 1


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_cross_tenant_events_do_not_leak(db_engine: AsyncEngine) -> None:
    world_a = await _seed_world(db_engine)
    world_b = await _seed_world(db_engine)
    now = datetime.now(UTC)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # Tenant B has loud events.
        for _ in range(5):
            await _insert_event(
                session,
                tenant_id=world_b["tenant_id"],
                campaign_id=world_b["campaign_id"],
                event_type=EventKind.click,
                plausible=True,
            )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        snap_a = await compute_campaign_kpis(
            session,
            tenant_id=world_a["tenant_id"],
            campaign_id=world_a["campaign_id"],
            now=now,
        )
    assert snap_a.kpis.clicks == 0


# ---------------------------------------------------------------------------
# Source freshness
# ---------------------------------------------------------------------------


async def test_source_freshness_reflects_most_recent_event(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    now = datetime.now(UTC)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # Plausible — 10 min ago.
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            event_type=EventKind.impression,
            plausible=True,
            event_at=now - timedelta(minutes=10),
        )
        # SendGrid — 2 min ago.
        await _insert_dispatch_attempt(
            session,
            tenant_id=world["tenant_id"],
            content_asset_id=world["asset_id"],
            provider_message_id="msg-fresh",
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            event_type=EventKind.open,
            sg_message_id="msg-fresh",
            event_at=now - timedelta(minutes=2),
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        snap = await compute_campaign_kpis(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            now=now,
        )

    by_name = {s.name: s for s in snap.sources}
    assert 595 <= by_name["plausible"].freshness_seconds <= 605
    assert 115 <= by_name["sendgrid"].freshness_seconds <= 125
    # Documented latencies surface alongside observed freshness.
    assert by_name["plausible"].documented_latency_seconds == 300
    assert by_name["sendgrid"].documented_latency_seconds == 60


# ---------------------------------------------------------------------------
# HTTP endpoint
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
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@kpi.test",
                role=role,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
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


async def test_endpoint_returns_snapshot(client_as, world, db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            event_type=EventKind.click,
            plausible=True,
        )

    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/campaigns/{world['campaign_id']}/kpis")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == str(world["campaign_id"])
    assert body["kpis"]["clicks"] == 1
    assert body["kpis"]["derived"]["ctr"] == "0.0000"  # no impressions → 0
    assert {s["name"] for s in body["sources"]} == {"plausible"}


async def test_endpoint_returns_404_for_other_tenants_campaign(
    client_as, db_engine: AsyncEngine
) -> None:
    other = await _seed_world(db_engine)
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/campaigns/{other['campaign_id']}/kpis")
    assert resp.status_code == 404


async def test_endpoint_returns_404_for_unknown_campaign(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/campaigns/{uuid.uuid4()}/kpis")
    assert resp.status_code == 404


async def test_channel_filter_via_query_string(
    client_as, world, db_engine: AsyncEngine
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            channel_id=world["email_channel_id"],
            event_type=EventKind.impression,
            plausible=True,
        )
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            channel_id=world["linkedin_channel_id"],
            event_type=EventKind.impression,
            plausible=True,
        )

    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(
        f"/api/campaigns/{world['campaign_id']}/kpis",
        params={"channel_id": str(world["linkedin_channel_id"])},
    )
    body = resp.json()
    assert body["kpis"]["impressions"] == 1
