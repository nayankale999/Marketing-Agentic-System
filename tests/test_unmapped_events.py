"""W33 — Admin unmapped-webhook surface (E12-S06 #3).

Coverage:
  * Admin sees only unmapped rows (mapped_event_id IS NULL).
  * Provider filter restricts the result set.
  * `total` reflects the full count, not just the page.
  * Non-admin roles → 403.
  * Limit clamps page size.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import EventKind, UserRole
from app.db.models import AnalyticEvent, AppUser, RawWebhook, Tenant


async def _seed_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"uw-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@uw.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


async def _insert_raw(
    engine: AsyncEngine,
    tenant_id: uuid.UUID,
    provider: str,
    *,
    mapped: bool = False,
    signature_valid: bool = True,
) -> uuid.UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        mapped_id: uuid.UUID | None = None
        if mapped:
            event = AnalyticEvent(
                tenant_id=tenant_id,
                event_type=EventKind.open,
                payload={},
            )
            session.add(event)
            await session.flush()
            mapped_id = event.id
        row = RawWebhook(
            tenant_id=tenant_id,
            provider=provider,
            signature_valid=signature_valid,
            signature_reason=None if signature_valid else "secret_mismatch",
            headers={"content-type": "application/json"},
            payload=b"{}",
            mapped_event_id=mapped_id,
        )
        session.add(row)
        await session.flush()
        return row.id


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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_lists_only_unmapped_rows(
    client_as, tenant_id, db_engine: AsyncEngine
) -> None:
    # Two unmapped, one mapped — mapped one must be excluded.
    await _insert_raw(db_engine, tenant_id, "sendgrid", mapped=False)
    await _insert_raw(db_engine, tenant_id, "linkedin", mapped=False)
    await _insert_raw(db_engine, tenant_id, "sendgrid", mapped=True)

    client, _ = await client_as(UserRole.admin)
    resp = await client.get("/api/webhooks/unmapped")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    providers = {item["provider"] for item in body["items"]}
    assert providers == {"sendgrid", "linkedin"}


async def test_provider_filter_restricts_results(
    client_as, tenant_id, db_engine: AsyncEngine
) -> None:
    await _insert_raw(db_engine, tenant_id, "sendgrid", mapped=False)
    await _insert_raw(db_engine, tenant_id, "sendgrid", mapped=False)
    await _insert_raw(db_engine, tenant_id, "linkedin", mapped=False)

    client, _ = await client_as(UserRole.admin)
    resp = await client.get("/api/webhooks/unmapped", params={"provider": "linkedin"})
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["provider"] == "linkedin"


async def test_total_reflects_full_count_not_page(
    client_as, tenant_id, db_engine: AsyncEngine
) -> None:
    for _ in range(5):
        await _insert_raw(db_engine, tenant_id, "sendgrid", mapped=False)

    client, _ = await client_as(UserRole.admin)
    resp = await client.get("/api/webhooks/unmapped", params={"limit": 2})
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


async def test_invalid_signature_unmapped_rows_surface(
    client_as, tenant_id, db_engine: AsyncEngine
) -> None:
    """Rows persisted on signature failure (still mapped_event_id=NULL) must
    appear in the admin list so an operator can diagnose secret-rotation
    incidents."""
    await _insert_raw(
        db_engine, tenant_id, "sendgrid", mapped=False, signature_valid=False
    )
    client, _ = await client_as(UserRole.admin)
    resp = await client.get("/api/webhooks/unmapped")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["signature_valid"] is False
    assert body["items"][0]["signature_reason"] == "secret_mismatch"


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role", [UserRole.manager, UserRole.marketer, UserRole.viewer]
)
async def test_non_admin_cannot_list_unmapped(client_as, role) -> None:
    client, _ = await client_as(role)
    resp = await client.get("/api/webhooks/unmapped")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_limit_out_of_range_returns_422(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    assert (
        await client.get("/api/webhooks/unmapped", params={"limit": 0})
    ).status_code == 422
    assert (
        await client.get("/api/webhooks/unmapped", params={"limit": 201})
    ).status_code == 422
