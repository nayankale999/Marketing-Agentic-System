"""W41 — Custom KPI + reconciliation HTTP endpoints (E10-S07 / E10-S06)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AnalyticEvent,
    AppUser,
    Campaign,
    CampaignChannelBudget,
    Channel,
    CustomKpi,
    Tenant,
)
from app.db.enums import EventKind


async def _seed(db_engine: AsyncEngine) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ck-ep-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@ckep.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        ch = Channel(
            tenant_id=tenant.id,
            name="Email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        session.add(ch)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("1000"),
            currency="USD",
            start_date=date.today() - timedelta(days=10),
            end_date=date.today(),
            brief="b",
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "channel_id": ch.id,
            "owner_id": owner.id,
        }


@pytest.fixture
async def world(override_api_db, db_engine: AsyncEngine) -> dict:
    return await _seed(db_engine)


@pytest.fixture
async def client_as(world, db_engine: AsyncEngine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            user = AppUser(
                tenant_id=world["tenant_id"],
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@ckep.test",
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


# ---------------------------------------------------------------------------
# Custom KPI CRUD
# ---------------------------------------------------------------------------


async def test_create_custom_kpi_returns_201(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(
        "/api/custom-kpis",
        json={
            "name": "demo_clicks",
            "campaign_id": str(world["campaign_id"]),
            "formula": {
                "event_type": "click",
                "filters": [{"path": "payload.utm_content", "op": "eq", "value": "demo"}],
            },
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "demo_clicks"
    assert body["deleted_at"] is None


async def test_create_rejects_missing_event_type(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(
        "/api/custom-kpis",
        json={"name": "broken", "formula": {}},
    )
    assert resp.status_code == 422


async def test_delete_is_soft_delete(client_as, world, db_engine: AsyncEngine) -> None:
    client, _ = await client_as(UserRole.marketer)
    create = await client.post(
        "/api/custom-kpis",
        json={"name": "k", "formula": {"event_type": "click"}},
    )
    kpi_id = create.json()["id"]
    delete = await client.delete(f"/api/custom-kpis/{kpi_id}")
    assert delete.status_code == 200
    assert delete.json()["deleted_at"] is not None

    # Default list hides it; include_deleted=true shows it.
    listed = await client.get("/api/custom-kpis")
    assert all(item["id"] != kpi_id for item in listed.json()["items"])
    listed_all = await client.get(
        "/api/custom-kpis", params={"include_deleted": "true"}
    )
    assert any(item["id"] == kpi_id for item in listed_all.json()["items"])


async def test_viewer_cannot_create(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post(
        "/api/custom-kpis",
        json={"name": "k", "formula": {"event_type": "click"}},
    )
    assert resp.status_code == 403


async def test_campaign_custom_kpis_evaluation_endpoint(
    client_as, world, db_engine: AsyncEngine
) -> None:
    # Create a KPI via the API, then seed events that match.
    client, _ = await client_as(UserRole.marketer)
    await client.post(
        "/api/custom-kpis",
        json={
            "name": "demo_clicks",
            "campaign_id": str(world["campaign_id"]),
            "formula": {
                "event_type": "click",
                "filters": [
                    {"path": "payload.utm_content", "op": "eq", "value": "demo"}
                ],
            },
        },
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for _ in range(4):
            session.add(
                AnalyticEvent(
                    tenant_id=world["tenant_id"],
                    campaign_id=world["campaign_id"],
                    event_type=EventKind.click,
                    payload={"utm_content": "demo"},
                    provider_event_id=f"e-{uuid.uuid4().hex[:10]}",
                )
            )

    resp = await client.get(
        f"/api/campaigns/{world['campaign_id']}/custom-kpis"
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["value"] == 4
    assert items[0]["missing_event"] is False


# ---------------------------------------------------------------------------
# Spend reconciliation endpoints
# ---------------------------------------------------------------------------


async def test_admin_can_run_reconciliation(
    client_as, world, db_engine: AsyncEngine
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            CampaignChannelBudget(
                campaign_id=world["campaign_id"],
                channel_id=world["channel_id"],
                allocated=Decimal("1000"),
                spent=Decimal("500.00"),
            )
        )

    client, _ = await client_as(UserRole.admin)
    resp = await client.post(
        "/api/spend-reconciliation/run",
        json={
            "period_start": (date.today() - timedelta(days=30)).isoformat(),
            "period_end": date.today().isoformat(),
            "invoices": {str(world["campaign_id"]): "550.00"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "pending"


async def test_marketer_cannot_run_reconciliation(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(
        "/api/spend-reconciliation/run",
        json={
            "period_start": (date.today() - timedelta(days=30)).isoformat(),
            "period_end": date.today().isoformat(),
            "invoices": {str(world["campaign_id"]): "1"},
        },
    )
    assert resp.status_code == 403


async def test_explain_and_dispute_flow(
    client_as, world, db_engine: AsyncEngine
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            CampaignChannelBudget(
                campaign_id=world["campaign_id"],
                channel_id=world["channel_id"],
                allocated=Decimal("1000"),
                spent=Decimal("500.00"),
            )
        )

    client, _ = await client_as(UserRole.admin)
    run_resp = await client.post(
        "/api/spend-reconciliation/run",
        json={
            "period_start": (date.today() - timedelta(days=30)).isoformat(),
            "period_end": date.today().isoformat(),
            "invoices": {str(world["campaign_id"]): "525.00"},
        },
    )
    recon_id = run_resp.json()["items"][0]["id"]

    explained = await client.post(
        f"/api/spend-reconciliation/{recon_id}/explain",
        json={"note": "late charge"},
    )
    assert explained.status_code == 200
    assert explained.json()["status"] == "explained"
    assert explained.json()["note"] == "late charge"

    disputed = await client.post(
        f"/api/spend-reconciliation/{recon_id}/dispute",
        json={"note": "platform error"},
    )
    assert disputed.status_code == 200
    assert disputed.json()["status"] == "disputed"
