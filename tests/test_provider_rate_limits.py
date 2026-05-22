"""W31 — Provider rate limit admin CRUD (E08-S06 partial)."""

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
from app.db.models import AppUser, ProviderRateLimit, Tenant


async def _seed_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"rl-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@rl.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def tenant_id(override_api_db, db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_tenant(db_engine)


@pytest.fixture
async def client_as(tenant_id, db_engine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, tenant_id, role)
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


async def test_put_and_get_rate_limit(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.put(
        "/api/provider-rate-limits/sendgrid",
        json={"requests_per_minute": 60, "enabled": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "sendgrid"
    assert body["requests_per_minute"] == 60
    assert body["enabled"] is True

    fetched = await client.get("/api/provider-rate-limits/sendgrid")
    assert fetched.status_code == 200
    assert fetched.json()["requests_per_minute"] == 60


async def test_put_upserts_existing_row(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    r1 = await client.put(
        "/api/provider-rate-limits/sendgrid",
        json={"requests_per_minute": 60, "enabled": False},
    )
    r2 = await client.put(
        "/api/provider-rate-limits/sendgrid",
        json={"requests_per_minute": 120, "enabled": True},
    )
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["requests_per_minute"] == 120
    assert r2.json()["enabled"] is True


async def test_list_returns_all_configured(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    await client.put(
        "/api/provider-rate-limits/sendgrid",
        json={"requests_per_minute": 60, "enabled": True},
    )
    await client.put(
        "/api/provider-rate-limits/linkedin",
        json={"requests_per_minute": 30, "enabled": False},
    )
    resp = await client.get("/api/provider-rate-limits")
    body = resp.json()
    assert body["total"] == 2
    providers = {item["provider"] for item in body["items"]}
    assert providers == {"sendgrid", "linkedin"}


async def test_get_unconfigured_returns_404(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.get("/api/provider-rate-limits/sendgrid")
    assert resp.status_code == 404


async def test_delete_removes_row(client_as, db_engine: AsyncEngine) -> None:
    client, _ = await client_as(UserRole.admin)
    await client.put(
        "/api/provider-rate-limits/sendgrid",
        json={"requests_per_minute": 60, "enabled": True},
    )
    resp = await client.delete("/api/provider-rate-limits/sendgrid")
    assert resp.status_code == 204
    assert (await client.get("/api/provider-rate-limits/sendgrid")).status_code == 404


async def test_rejects_invalid_rpm(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.put(
        "/api/provider-rate-limits/sendgrid",
        json={"requests_per_minute": 0, "enabled": True},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("role", [UserRole.manager, UserRole.marketer, UserRole.viewer])
async def test_non_admin_cannot_manage_rate_limits(client_as, role) -> None:
    client, _ = await client_as(role)
    assert (await client.get("/api/provider-rate-limits")).status_code == 403
    assert (
        await client.put(
            "/api/provider-rate-limits/sendgrid",
            json={"requests_per_minute": 1, "enabled": True},
        )
    ).status_code == 403
    assert (
        await client.delete("/api/provider-rate-limits/sendgrid")
    ).status_code == 403
