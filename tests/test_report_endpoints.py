"""W38 — Campaign report HTTP endpoints (E10-S04 / E13-S04)."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    CampaignStatus,
    CampaignType,
    UserRole,
)
from app.db.models import (
    AppUser,
    Campaign,
    Tenant,
)


async def _seed(db_engine: AsyncEngine) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"rpt-ep-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@rptep.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="ep test",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("100"),
            currency="USD",
            start_date=date.today() - timedelta(days=10),
            end_date=date.today(),
            brief="b",
            status=CampaignStatus.live,
            kpi_targets={"primary": {"metric": "click", "target": 10}, "secondary": []},
        )
        session.add(campaign)
        await session.flush()
        return {"tenant_id": tenant.id, "campaign_id": campaign.id}


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
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@rptep.test",
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
# Generate + fetch
# ---------------------------------------------------------------------------


async def test_generate_then_list_then_fetch_latest(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    gen = await client.post(f"/api/campaigns/{world['campaign_id']}/reports")
    assert gen.status_code == 201, gen.text
    body = gen.json()
    assert body["version"] == 1
    assert body["is_latest"] is True

    lst = await client.get(f"/api/campaigns/{world['campaign_id']}/reports")
    assert lst.json()["total"] == 1

    latest = await client.get(f"/api/campaigns/{world['campaign_id']}/reports/latest")
    assert latest.json()["id"] == body["id"]


async def test_regenerate_versions_increment(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    await client.post(f"/api/campaigns/{world['campaign_id']}/reports")
    second = await client.post(f"/api/campaigns/{world['campaign_id']}/reports")
    assert second.json()["version"] == 2
    assert second.json()["is_latest"] is True


async def test_csv_endpoint_returns_text_csv(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    await client.post(f"/api/campaigns/{world['campaign_id']}/reports")
    csv_resp = await client.get(
        f"/api/campaigns/{world['campaign_id']}/reports/latest.csv"
    )
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(csv_resp.text)))
    assert rows[0] == ["section", "key", "value"]
    assert any(r[0] == "objectives" for r in rows[1:])


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def test_latest_returns_404_when_no_reports(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    r = await client.get(f"/api/campaigns/{world['campaign_id']}/reports/latest")
    assert r.status_code == 404


async def test_viewer_cannot_generate(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    r = await client.post(f"/api/campaigns/{world['campaign_id']}/reports")
    assert r.status_code == 403


async def test_cross_tenant_returns_404(client_as, db_engine: AsyncEngine) -> None:
    other = await _seed(db_engine)
    client, _ = await client_as(UserRole.viewer)
    r = await client.get(f"/api/campaigns/{other['campaign_id']}/reports")
    assert r.status_code == 404
