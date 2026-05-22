"""W29 — Public unsubscribe endpoint (E16-S04 #2/#3).

Token lifecycle:
  * valid token → suppression row written + audit (within milliseconds, well
    under the 5s AC)
  * second call with same token → idempotent (`already_existed=true`)
  * invalid / tampered token → 404 with the same opaque message
  * suppression is per channel_platform — an email unsubscribe doesn't
    suppress sms for the same identifier (E16-S04 #3)

Tests admin CRUD on /api/compliance-settings live here too since they
share the same data model.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from itsdangerous import URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import ChannelPlatform, UserRole
from app.db.models import (
    AppUser,
    AuditLog,
    SuppressionEntry,
    Tenant,
    TenantComplianceSettings,
)


_SALT = "email-unsubscribe"


async def _seed_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"unsub-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _set_unsub_secret(
    db_engine: AsyncEngine, tenant_id: uuid.UUID, secret: str
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            TenantComplianceSettings(
                tenant_id=tenant_id,
                postal_address="123 Acme Way, City",
                unsubscribe_secret=secret,
            )
        )


def _sign_token(secret: str, payload: dict) -> str:
    return URLSafeSerializer(secret_key=secret, salt=_SALT).dumps(payload)


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@unsub.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def public_client(override_api_db) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Public unsubscribe token endpoint
# ---------------------------------------------------------------------------


async def test_valid_token_creates_suppression(
    public_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_unsub_secret(db_engine, tenant_id, "tenant-secret-A")

    token = _sign_token(
        "tenant-secret-A",
        {
            "tenant_id": str(tenant_id),
            "channel_platform": "email",
            "identifier": "user@customer.com",
        },
    )
    resp = await public_client.post(f"/api/unsubscribe/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["identifier"] == "user@customer.com"
    assert body["already_existed"] is False

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.tenant_id == tenant_id,
                    SuppressionEntry.identifier == "user@customer.com",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].reason == "unsubscribe"


async def test_second_call_is_idempotent(
    public_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_unsub_secret(db_engine, tenant_id, "tenant-secret-B")
    token = _sign_token(
        "tenant-secret-B",
        {
            "tenant_id": str(tenant_id),
            "channel_platform": "email",
            "identifier": "twice@customer.com",
        },
    )

    r1 = await public_client.post(f"/api/unsubscribe/{token}")
    r2 = await public_client.post(f"/api/unsubscribe/{token}")
    assert r1.json()["already_existed"] is False
    assert r2.json()["already_existed"] is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.tenant_id == tenant_id,
                    SuppressionEntry.identifier == "twice@customer.com",
                )
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_per_channel_suppression(
    public_client, db_engine: AsyncEngine
) -> None:
    """E16-S04 #3: an email unsubscribe doesn't suppress the same identifier
    on other channels."""
    tenant_id = await _seed_tenant(db_engine)
    await _set_unsub_secret(db_engine, tenant_id, "tenant-secret-C")
    secret = "tenant-secret-C"

    email_token = _sign_token(secret, {
        "tenant_id": str(tenant_id),
        "channel_platform": "email",
        "identifier": "person@customer.com",
    })
    await public_client.post(f"/api/unsubscribe/{email_token}")

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.tenant_id == tenant_id,
                    SuppressionEntry.identifier == "person@customer.com",
                )
            )
        ).scalars().all()
        assert {r.channel_platform for r in rows} == {ChannelPlatform.email}
        # No sms / linkedin row even though they share identifier.


async def test_tampered_token_returns_404(public_client) -> None:
    resp = await public_client.post("/api/unsubscribe/not-a-real-token")
    assert resp.status_code == 404


async def test_token_signed_with_wrong_secret_returns_404(
    public_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_unsub_secret(db_engine, tenant_id, "right-secret")

    token = _sign_token(
        "WRONG-secret",
        {
            "tenant_id": str(tenant_id),
            "channel_platform": "email",
            "identifier": "x@y.com",
        },
    )
    resp = await public_client.post(f"/api/unsubscribe/{token}")
    assert resp.status_code == 404


async def test_unsubscribe_writes_audit_row(
    public_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_unsub_secret(db_engine, tenant_id, "secret-AU")
    token = _sign_token("secret-AU", {
        "tenant_id": str(tenant_id),
        "channel_platform": "email",
        "identifier": "audit@customer.com",
    })
    await public_client.post(f"/api/unsubscribe/{token}")

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "suppression_entry",
                    AuditLog.action == "unsubscribed",
                    AuditLog.tenant_id == tenant_id,
                )
            )
        ).scalars().all()
        # At least one audit row exists for this tenant — testcontainer is
        # session-scoped so other tests might have added rows too, but we
        # filter by tenant_id so this is clean.
        assert any(
            a.after_state and a.after_state.get("identifier") == "audit@customer.com"
            for a in audits
        )


# ---------------------------------------------------------------------------
# /api/compliance-settings admin CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_client(
    override_api_db, db_engine: AsyncEngine
) -> AsyncIterator[httpx.AsyncClient]:
    tenant_id = await _seed_tenant(db_engine)
    user = await _make_user(db_engine, tenant_id, UserRole.admin)
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield client
        finally:
            app.dependency_overrides.pop(get_current_user, None)


async def test_get_compliance_settings_defaults_when_unset(admin_client) -> None:
    resp = await admin_client.get("/api/compliance-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["postal_address"] is None
    assert body["has_unsubscribe_secret"] is False


async def test_put_compliance_settings_upserts(admin_client) -> None:
    r1 = await admin_client.put(
        "/api/compliance-settings",
        json={"postal_address": "1 First St", "unsubscribe_secret": "supersecret"},
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["postal_address"] == "1 First St"
    assert body["has_unsubscribe_secret"] is True
    # `unsubscribe_secret` is never echoed back.
    assert "unsubscribe_secret" not in body

    r2 = await admin_client.put(
        "/api/compliance-settings",
        json={"postal_address": "2 Second Ave"},  # secret omitted, kept
    )
    assert r2.json()["postal_address"] == "2 Second Ave"
    assert r2.json()["has_unsubscribe_secret"] is True


async def test_compliance_settings_admin_only(
    override_api_db, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    user = await _make_user(db_engine, tenant_id, UserRole.marketer)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/compliance-settings")).status_code == 403
            assert (
                await client.put(
                    "/api/compliance-settings",
                    json={"postal_address": "x"},
                )
            ).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
