"""W10: Campaign CRUD API (E03-S01, E03-S02, E03-S06)."""

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import CampaignStatus, CampaignType, UserRole
from app.db.models import AppUser, AuditLog, Campaign, Tenant


@pytest.fixture
async def tenant_in_db(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"camp-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _create_user(engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole) -> AppUser:
    """Persist a real AppUser row so FKs (campaign.owner_id) can reference it."""
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@camp.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def client_as(
    override_api_db,
    db_engine: AsyncEngine,
    tenant_in_db: uuid.UUID,
) -> AsyncIterator:
    """Yields an async factory: `await client_as(role)` -> (client, user).

    The user is inserted into the DB so `campaign.owner_id` FK is satisfiable.
    """
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _create_user(db_engine, tenant_in_db, role)
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


def _valid_create_body() -> dict:
    return {
        "name": f"Q3 Launch {uuid.uuid4().hex[:6]}",
        "campaign_type": CampaignType.product_launch.value,
        "objective": "Increase awareness in the EU region",
        "start_date": date.today().isoformat(),
        "end_date": (date.today() + timedelta(days=30)).isoformat(),
        "budget_total": "12500.00",
        "currency": "EUR",
        "brief": "Beta release, focus on SMB buyers",
        "kpi_targets": {"primary": "MQLs", "target": 500},
    }


# -- Create ------------------------------------------------------------------


async def test_create_campaign_returns_201_with_drafted_status(
    client_as, db_engine: AsyncEngine, tenant_in_db: uuid.UUID
) -> None:
    client, user = await client_as(UserRole.marketer)
    resp = await client.post("/api/campaigns", json=_valid_create_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "drafted"
    assert body["tenant_id"] == str(tenant_in_db)
    assert body["owner_id"] == str(user.id)
    assert body["currency"] == "EUR"
    assert body["kpi_targets"] == {"primary": "MQLs", "target": 500}


async def test_create_campaign_writes_audit_log_row(
    client_as, db_engine: AsyncEngine, tenant_in_db: uuid.UUID
) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post("/api/campaigns", json=_valid_create_body())
    campaign_id = uuid.UUID(resp.json()["id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.entity_id == campaign_id)))
            .scalars()
            .all()
        )
    actions = sorted(r.action for r in rows)
    assert "created" in actions


async def test_create_campaign_end_before_start_returns_422(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    body = _valid_create_body()
    body["end_date"] = (date.today() - timedelta(days=1)).isoformat()
    resp = await client.post("/api/campaigns", json=body)
    assert resp.status_code == 422
    assert "end_date" in resp.json()["detail"]


async def test_create_campaign_missing_required_returns_422(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    body = _valid_create_body()
    del body["objective"]
    resp = await client.post("/api/campaigns", json=body)
    assert resp.status_code == 422


async def test_create_requires_marketer_role(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post("/api/campaigns", json=_valid_create_body())
    assert resp.status_code == 403


# -- Get one + list ----------------------------------------------------------


async def test_get_campaign_returns_what_was_created(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    created = (await client.post("/api/campaigns", json=_valid_create_body())).json()
    fetched = await client.get(f"/api/campaigns/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
    assert fetched.json()["name"] == created["name"]


async def test_get_unknown_campaign_returns_404(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/campaigns/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_viewer_can_list_campaigns(client_as) -> None:
    marketer_client, _ = await client_as(UserRole.marketer)
    for _ in range(3):
        await marketer_client.post("/api/campaigns", json=_valid_create_body())

    viewer_client, _ = await client_as(UserRole.viewer)
    resp = await viewer_client.get("/api/campaigns")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 3
    assert len(body["items"]) == body["total"]


async def test_list_filters_by_status(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    await client.post("/api/campaigns", json=_valid_create_body())
    resp = await client.get("/api/campaigns?status=drafted")
    assert resp.status_code == 200
    statuses = {row["status"] for row in resp.json()["items"]}
    assert statuses <= {"drafted"}


async def test_list_searches_by_name(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    body = _valid_create_body()
    body["name"] = f"unique-needle-{uuid.uuid4().hex[:8]}"
    await client.post("/api/campaigns", json=body)
    resp = await client.get("/api/campaigns?q=needle")
    items = resp.json()["items"]
    assert resp.status_code == 200
    assert any(item["name"].startswith("unique-needle-") for item in items)


async def test_list_pagination(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    for _ in range(5):
        await client.post("/api/campaigns", json=_valid_create_body())
    resp = await client.get("/api/campaigns?limit=2&offset=0")
    body = resp.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2


# -- Patch -------------------------------------------------------------------


async def test_patch_campaign_in_drafted_updates_fields(client_as, db_engine: AsyncEngine) -> None:
    client, _ = await client_as(UserRole.marketer)
    created = (await client.post("/api/campaigns", json=_valid_create_body())).json()
    patched = await client.patch(
        f"/api/campaigns/{created['id']}",
        json={"objective": "Pivot: target enterprise"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["objective"] == "Pivot: target enterprise"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == uuid.UUID(created["id"]),
                        AuditLog.action == "updated",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert audits[0].extra_metadata == {"fields": ["objective"]}
    assert audits[0].before_state["objective"] == created["objective"]
    assert audits[0].after_state["objective"] == "Pivot: target enterprise"


async def test_patch_campaign_blocked_when_live(client_as, db_engine: AsyncEngine) -> None:
    client, _ = await client_as(UserRole.marketer)
    created = (await client.post("/api/campaigns", json=_valid_create_body())).json()
    # Force the campaign into `live` via direct SQL (no live-launch endpoint in W10).
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        c = await session.get(Campaign, uuid.UUID(created["id"]))
        assert c is not None
        c.status = CampaignStatus.live

    resp = await client.patch(f"/api/campaigns/{created['id']}", json={"objective": "no go"})
    assert resp.status_code == 409
    assert "editing" in resp.json()["detail"].lower()


async def test_patch_unknown_campaign_returns_404(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.patch(f"/api/campaigns/{uuid.uuid4()}", json={"name": "nope"})
    assert resp.status_code == 404


async def test_patch_requires_marketer(client_as) -> None:
    marketer_client, _ = await client_as(UserRole.marketer)
    created = (await marketer_client.post("/api/campaigns", json=_valid_create_body())).json()

    viewer_client, _ = await client_as(UserRole.viewer)
    resp = await viewer_client.patch(f"/api/campaigns/{created['id']}", json={"name": "no perms"})
    assert resp.status_code == 403


async def test_patch_invalid_date_combo_returns_422(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    created = (await client.post("/api/campaigns", json=_valid_create_body())).json()
    resp = await client.patch(
        f"/api/campaigns/{created['id']}",
        json={"start_date": (date.today() + timedelta(days=60)).isoformat()},
    )
    assert resp.status_code == 422
    assert "end_date" in resp.json()["detail"]
