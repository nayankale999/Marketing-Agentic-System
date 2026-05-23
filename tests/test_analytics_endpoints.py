"""W37 — Anomaly + recommendation HTTP endpoints (E10-S02 / E10-S03).

  * GET  /api/campaigns/{id}/anomalies            list + include_dismissed filter
  * POST /api/anomalies/{id}/dismiss              admin only
  * GET  /api/campaigns/{id}/recommendations      uplift filter
  * POST /api/recommendations/{id}/accept         lifecycle
  * POST /api/recommendations/{id}/reject         lifecycle
"""

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
    EventKind,
    UserRole,
)
from app.db.models import (
    AppUser,
    Campaign,
    MetricAnomaly,
    OptimisationRecommendation,
    Tenant,
)


async def _seed(db_engine: AsyncEngine) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ana-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@ana.test",
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
            start_date=date.today() - timedelta(days=10),
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
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@ana.test",
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


async def _seed_anomaly(
    db_engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    dismissed: bool = False,
    severity: str = "warning",
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        anomaly = MetricAnomaly(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            metric=EventKind.open.value,
            window_start=datetime.now(UTC) - timedelta(days=1),
            window_end=datetime.now(UTC),
            observed_value=Decimal("100"),
            baseline_median=Decimal("10"),
            baseline_stddev=Decimal("2"),
            sigma=Decimal("9.0"),
            severity=severity,
            dismissed_at=datetime.now(UTC) if dismissed else None,
        )
        session.add(anomaly)
        await session.flush()
        return anomaly.id


async def _seed_recommendation(
    db_engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    predicted_uplift: Decimal | None = Decimal("0.10"),
    status: str = "pending",
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        rec = OptimisationRecommendation(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            kind="budget_shift",
            proposal={
                "from": {"channel": "LinkedIn", "allocation_pct": 50},
                "to": {"channel": "Email", "allocation_pct": 50},
                "shifted_pct": 10,
            },
            rationale="LinkedIn weak vs Email",
            predicted_uplift=predicted_uplift,
            status=status,
        )
        session.add(rec)
        await session.flush()
        return rec.id


# ---------------------------------------------------------------------------
# Anomaly endpoints
# ---------------------------------------------------------------------------


async def test_list_anomalies_filters_dismissed_by_default(
    client_as, world, db_engine: AsyncEngine
) -> None:
    await _seed_anomaly(
        db_engine, tenant_id=world["tenant_id"], campaign_id=world["campaign_id"]
    )
    await _seed_anomaly(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        dismissed=True,
    )
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/campaigns/{world['campaign_id']}/anomalies")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1

    resp_all = await client.get(
        f"/api/campaigns/{world['campaign_id']}/anomalies",
        params={"include_dismissed": "true"},
    )
    assert resp_all.json()["total"] == 2


async def test_only_admin_can_dismiss(client_as, world, db_engine: AsyncEngine) -> None:
    anomaly_id = await _seed_anomaly(
        db_engine, tenant_id=world["tenant_id"], campaign_id=world["campaign_id"]
    )
    marketer, _ = await client_as(UserRole.marketer)
    r = await marketer.post(f"/api/anomalies/{anomaly_id}/dismiss")
    assert r.status_code == 403

    admin, _ = await client_as(UserRole.admin)
    r2 = await admin.post(f"/api/anomalies/{anomaly_id}/dismiss")
    assert r2.status_code == 200
    assert r2.json()["dismissed_at"] is not None


async def test_anomalies_404_for_other_tenants_campaign(
    client_as, db_engine: AsyncEngine
) -> None:
    other = await _seed(db_engine)
    client, _ = await client_as(UserRole.viewer)
    r = await client.get(f"/api/campaigns/{other['campaign_id']}/anomalies")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Recommendation endpoints
# ---------------------------------------------------------------------------


async def test_recommendations_filter_below_uplift_by_default(
    client_as, world, db_engine: AsyncEngine
) -> None:
    high = await _seed_recommendation(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        predicted_uplift=Decimal("0.10"),
    )
    low = await _seed_recommendation(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
        predicted_uplift=Decimal("0.02"),
    )
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(
        f"/api/campaigns/{world['campaign_id']}/recommendations"
    )
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(high) in ids
    assert str(low) not in ids

    resp_all = await client.get(
        f"/api/campaigns/{world['campaign_id']}/recommendations",
        params={"include_low_uplift": "true"},
    )
    ids_all = {item["id"] for item in resp_all.json()["items"]}
    assert str(low) in ids_all


async def test_accept_recommendation_records_applied_columns(
    client_as, world, db_engine: AsyncEngine
) -> None:
    rec_id = await _seed_recommendation(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
    )
    client, user = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/recommendations/{rec_id}/accept")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["applied_at"] is not None
    assert body["applied_by"] == str(user.id)


async def test_reject_recommendation_lifecycle(
    client_as, world, db_engine: AsyncEngine
) -> None:
    rec_id = await _seed_recommendation(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
    )
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/recommendations/{rec_id}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    # Re-accepting after a reject is a 409.
    resp2 = await client.post(f"/api/recommendations/{rec_id}/accept")
    assert resp2.status_code == 409


async def test_viewer_cannot_accept(client_as, world, db_engine: AsyncEngine) -> None:
    rec_id = await _seed_recommendation(
        db_engine,
        tenant_id=world["tenant_id"],
        campaign_id=world["campaign_id"],
    )
    client, _ = await client_as(UserRole.viewer)
    r = await client.post(f"/api/recommendations/{rec_id}/accept")
    assert r.status_code == 403
