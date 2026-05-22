"""W20 — Tenant constraints CRUD (E05-S05).

Admin-only. The Strategist consumes these rows but doesn't write them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import UserRole
from app.db.models import AppUser, Tenant, TenantConstraint


@pytest.fixture
async def tenant_in_db(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"tc-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@tc.test",
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
    clients: list[httpx.AsyncClient] = []

    async def _factory(
        role: UserRole, *, tenant_id: uuid.UUID | None = None
    ) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, tenant_id or tenant_in_db, role)
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


# ---- Create ---------------------------------------------------------------


async def test_admin_can_create_forbid_channel(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.post(
        "/api/tenant-constraints",
        json={"kind": "forbid_channel", "payload": {"platform": "sms"}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "forbid_channel"
    assert body["payload"] == {"platform": "sms"}


async def test_admin_can_create_hard_cap(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.post(
        "/api/tenant-constraints",
        json={
            "kind": "hard_cap",
            "payload": {"platform": "email", "per": "week", "limit": 5},
        },
    )
    assert resp.status_code == 201


@pytest.mark.parametrize(
    "payload",
    [
        {"platform": "not_a_real_platform"},
        {},  # missing platform
    ],
)
async def test_invalid_forbid_channel_payload_returns_422(client_as, payload) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.post(
        "/api/tenant-constraints",
        json={"kind": "forbid_channel", "payload": payload},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"platform": "email", "per": "fortnight", "limit": 5},  # bad per
        {"platform": "email", "per": "week", "limit": 0},  # bad limit
        {"platform": "email", "per": "week", "limit": -3},  # bad limit
        {"platform": "fake", "per": "week", "limit": 1},  # bad platform
    ],
)
async def test_invalid_hard_cap_payload_returns_422(client_as, payload) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.post(
        "/api/tenant-constraints",
        json={"kind": "hard_cap", "payload": payload},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.manager, UserRole.viewer])
async def test_non_admin_cannot_create(client_as, role) -> None:
    client, _ = await client_as(role)
    resp = await client.post(
        "/api/tenant-constraints",
        json={"kind": "forbid_channel", "payload": {"platform": "sms"}},
    )
    assert resp.status_code == 403


# ---- List / Delete --------------------------------------------------------


async def test_list_returns_tenant_rows(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    await client.post(
        "/api/tenant-constraints",
        json={"kind": "forbid_channel", "payload": {"platform": "sms"}},
    )
    await client.post(
        "/api/tenant-constraints",
        json={
            "kind": "hard_cap",
            "payload": {"platform": "email", "per": "week", "limit": 5},
        },
    )
    resp = await client.get("/api/tenant-constraints")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


async def test_delete_removes_constraint(client_as, db_engine: AsyncEngine) -> None:
    client, _ = await client_as(UserRole.admin)
    created = (
        await client.post(
            "/api/tenant-constraints",
            json={"kind": "forbid_channel", "payload": {"platform": "sms"}},
        )
    ).json()

    resp = await client.delete(f"/api/tenant-constraints/{created['id']}")
    assert resp.status_code == 204

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        remaining = (
            await session.execute(
                select(TenantConstraint).where(
                    TenantConstraint.id == uuid.UUID(created["id"])
                )
            )
        ).scalar_one_or_none()
        assert remaining is None


# ---- RLS ------------------------------------------------------------------


async def test_constraints_isolated_per_tenant(
    client_as, db_engine: AsyncEngine
) -> None:
    client_a, _ = await client_as(UserRole.admin)
    await client_a.post(
        "/api/tenant-constraints",
        json={"kind": "forbid_channel", "payload": {"platform": "sms"}},
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        other = Tenant(name=f"other-{uuid.uuid4().hex[:6]}")
        session.add(other)
        await session.flush()
        other_id = other.id

    client_b, _ = await client_as(UserRole.admin, tenant_id=other_id)
    resp = await client_b.get("/api/tenant-constraints")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
