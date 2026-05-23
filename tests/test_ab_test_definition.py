"""W35 — A/B test definition + launch endpoints (E09-S01).

Covers the HTTP-shaped contract:
  * POST /api/campaigns/{id}/ab-tests creates a designing test.
  * Validation: split must sum to 100; variants must belong to campaign;
    second active test on same family is rejected.
  * POST /api/ab-tests/{id}/launch flips designing → running when every
    variant is approved/scheduled.
  * Launch is blocked when any variant is in pending_approval / drafted.
  * POST /api/ab-tests/{id}/stop ends without auto-setting a winner.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    UserRole,
)
from app.db.models import (
    AppUser,
    Campaign,
    ContentAsset,
    Tenant,
)


async def _seed(
    db_engine: AsyncEngine,
    *,
    variant_statuses: list[AssetStatus] | None = None,
) -> dict:
    statuses = variant_statuses or [AssetStatus.approved, AssetStatus.approved]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"def-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("0"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today(),
            brief="b",
            status=CampaignStatus.audience_built,
        )
        session.add(campaign)
        await session.flush()
        variants = []
        for s in statuses:
            v = ContentAsset(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                asset_type=AssetType.email,
                status=s,
                content="v",
            )
            session.add(v)
            await session.flush()
            variants.append(v)
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "variant_ids": [v.id for v in variants],
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
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@def.test",
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


def _create_body(world: dict, *, split: dict | None = None) -> dict:
    v_ids = [str(x) for x in world["variant_ids"]]
    s = split or {v_ids[0]: 50, v_ids[1]: 50}
    return {
        "name": "Subject A vs B",
        "hypothesis": "Question marks win",
        "primary_metric": "open",
        "variant_ids": v_ids,
        "traffic_split": s,
        "min_runtime_hours": 24,
        "max_runtime_hours": 168,
    }


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_ab_test_returns_designing(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests",
        json=_create_body(world),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == AbTestStatus.designing.value
    assert body["variant_a_id"] == str(world["variant_ids"][0])
    assert body["traffic_split"] == {
        str(world["variant_ids"][0]): 50,
        str(world["variant_ids"][1]): 50,
    }


async def test_create_rejects_split_not_summing_to_100(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    v_ids = [str(x) for x in world["variant_ids"]]
    resp = await client.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests",
        json=_create_body(world, split={v_ids[0]: 70, v_ids[1]: 20}),
    )
    assert resp.status_code == 422
    assert "100" in resp.json()["detail"]


async def test_create_rejects_variants_from_other_campaign(
    client_as, world, db_engine: AsyncEngine
) -> None:
    other = await _seed(db_engine)
    client, _ = await client_as(UserRole.marketer)
    body = _create_body(world)
    body["variant_ids"] = [
        str(world["variant_ids"][0]),
        str(other["variant_ids"][0]),
    ]
    body["traffic_split"] = {body["variant_ids"][0]: 50, body["variant_ids"][1]: 50}
    resp = await client.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests", json=body
    )
    assert resp.status_code == 422


async def test_create_rejects_duplicate_active_test_on_same_family(
    client_as, world
) -> None:
    client, _ = await client_as(UserRole.marketer)
    body = _create_body(world)
    r1 = await client.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests", json=body
    )
    assert r1.status_code == 201
    r2 = await client.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests", json=body
    )
    assert r2.status_code == 409


async def test_viewer_cannot_create(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests",
        json=_create_body(world),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


async def test_launch_flips_designing_to_running(client_as, world) -> None:
    client, _ = await client_as(UserRole.marketer)
    create = await client.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests", json=_create_body(world)
    )
    ab_test_id = create.json()["id"]
    launch = await client.post(f"/api/ab-tests/{ab_test_id}/launch")
    assert launch.status_code == 200, launch.text
    body = launch.json()
    assert body["status"] == AbTestStatus.running.value
    assert body["started_at"] is not None


async def test_launch_blocked_when_variant_not_approved(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed(
        db_engine,
        variant_statuses=[AssetStatus.approved, AssetStatus.pending_approval],
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=world["tenant_id"],
            email=f"m-{uuid.uuid4().hex[:6]}@def.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            create = await client.post(
                f"/api/campaigns/{world['campaign_id']}/ab-tests",
                json=_create_body(world),
            )
            ab_test_id = create.json()["id"]
            launch = await client.post(f"/api/ab-tests/{ab_test_id}/launch")
            assert launch.status_code == 409
            detail = launch.json()["detail"]
            assert detail["reason"] == "variants_not_approved"
            assert "pending_approval" in detail["statuses"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def test_stop_running_test_clears_status(client_as, world) -> None:
    marketer, _ = await client_as(UserRole.marketer)
    create = await marketer.post(
        f"/api/campaigns/{world['campaign_id']}/ab-tests",
        json=_create_body(world),
    )
    ab_test_id = create.json()["id"]
    await marketer.post(f"/api/ab-tests/{ab_test_id}/launch")
    manager, _ = await client_as(UserRole.manager)
    stop = await manager.post(f"/api/ab-tests/{ab_test_id}/stop")
    assert stop.status_code == 200
    body = stop.json()
    assert body["status"] == AbTestStatus.stopped.value
    assert body["winner_id"] is None
    assert body["stopped_at"] is not None
