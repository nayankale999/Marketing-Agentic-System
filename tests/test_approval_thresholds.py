"""W26 — Spend-threshold gates on single approve (E07-S04).

Three layers:

  * Admin GET/PUT on /api/approval-settings.
  * Snapshot mechanic — submit_for_approval writes the threshold onto each
    asset's metadata; later approve calls read from the snapshot, not the
    live settings (E07-S04 #3).
  * Single approve gate — marketer/manager refused with 403 for high-spend
    campaigns; audit_log captures the applied threshold (E07-S04 #4).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AppUser,
    Audience,
    AuditLog,
    Campaign,
    Channel,
    ContentAsset,
    Tenant,
    TenantApprovalSettings,
)
from app.db.session import set_tenant_context
from app.orchestrator.state_machine import campaign_sm


async def _seed_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"thr-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _seed_campaign(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    budget: Decimal = Decimal("1000.00"),
    currency: str = "USD",
    state: CampaignStatus = CampaignStatus.content_in_production,
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for platform, name in [(ChannelPlatform.email, "Email")]:
            existing = (
                await session.execute(
                    select(Channel).where(
                        Channel.tenant_id == tenant_id, Channel.platform == platform
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Channel(
                        tenant_id=tenant_id, name=name, platform=platform, is_active=True
                    )
                )
        owner = AppUser(
            tenant_id=tenant_id,
            email=f"owner-{uuid.uuid4().hex[:6]}@thr.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant_id,
            owner_id=owner.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=budget,
            currency=currency,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=state,
        )
        session.add(campaign)
        await session.flush()
        session.add(
            Audience(
                tenant_id=tenant_id,
                campaign_id=campaign.id,
                name="seg",
                segment_criteria={},
                estimated_size=10,
                actual_size=10,
                refreshed_at=datetime.now(UTC),
            )
        )
        return campaign.id


async def _seed_drafted_asset(
    db_engine: AsyncEngine, tenant_id: uuid.UUID, campaign_id: uuid.UUID
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            asset_type=AssetType.email,
            status=AssetStatus.drafted,
            title="t",
            content="b",
            extra_metadata={"channel_platform": "email", "fields": {"subject": "s"}},
            is_required=True,
        )
        session.add(asset)
        await session.flush()
        return asset.id


async def _set_settings(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    admin_above: Decimal | None,
    auto_cap: Decimal = Decimal("0"),
    currency: str = "USD",
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            TenantApprovalSettings(
                tenant_id=tenant_id,
                admin_required_above_amount=admin_above,
                auto_approval_cap_amount=auto_cap,
                currency=currency,
            )
        )


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@thr.test",
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
async def client_as(tenant_id, db_engine: AsyncEngine) -> AsyncIterator:
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
# Settings GET / PUT
# ---------------------------------------------------------------------------


async def test_get_settings_returns_defaults_when_no_row(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.get("/api/approval-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["admin_required_above_amount"] is None
    assert body["auto_approval_cap_amount"] == "0.00"
    assert body["currency"] == "USD"


async def test_put_settings_upserts_then_returns_them(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    payload = {
        "admin_required_above_amount": "5000.00",
        "auto_approval_cap_amount": "1000.00",
        "currency": "EUR",
    }
    put_resp = await client.put("/api/approval-settings", json=payload)
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["admin_required_above_amount"] == "5000.00"
    assert body["auto_approval_cap_amount"] == "1000.00"
    assert body["currency"] == "EUR"

    # Second PUT updates the same row, doesn't create a new one.
    update = {**payload, "auto_approval_cap_amount": "2500.00"}
    put_again = (await client.put("/api/approval-settings", json=update)).json()
    assert put_again["id"] == body["id"]
    assert put_again["auto_approval_cap_amount"] == "2500.00"


@pytest.mark.parametrize("role", [UserRole.manager, UserRole.marketer, UserRole.viewer])
async def test_non_admin_cannot_get_or_put(client_as, role) -> None:
    client, _ = await client_as(role)
    assert (await client.get("/api/approval-settings")).status_code == 403
    assert (
        await client.put(
            "/api/approval-settings",
            json={"auto_approval_cap_amount": "100.00"},
        )
    ).status_code == 403


# ---------------------------------------------------------------------------
# Snapshot mechanic
# ---------------------------------------------------------------------------


async def test_submit_for_approval_writes_threshold_snapshot(
    override_api_db, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_settings(
        db_engine,
        tenant_id,
        admin_above=Decimal("5000.00"),
        auto_cap=Decimal("1000.00"),
        currency="USD",
    )
    campaign_id = await _seed_campaign(
        db_engine, tenant_id, budget=Decimal("8000.00"), currency="USD"
    )
    asset_id = await _seed_drafted_asset(db_engine, tenant_id, campaign_id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, asset_id)
        snap = asset.extra_metadata["approval_threshold"]
        assert snap["admin_required_above_amount"] == "5000.00"
        assert snap["auto_approval_cap_amount"] == "1000.00"
        assert snap["currency"] == "USD"
        assert "snapshot_taken_at" in snap


async def test_in_flight_uses_snapshot_not_latest_settings(
    override_api_db, db_engine: AsyncEngine
) -> None:
    """E07-S04 #3: change the threshold AFTER an asset enters the queue.
    The approve call must read the snapshot, not the new value."""
    tenant_id = await _seed_tenant(db_engine)
    await _set_settings(
        db_engine,
        tenant_id,
        admin_above=Decimal("5000.00"),
        currency="USD",
    )
    campaign_id = await _seed_campaign(
        db_engine, tenant_id, budget=Decimal("8000.00")
    )
    asset_id = await _seed_drafted_asset(db_engine, tenant_id, campaign_id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")

    # Admin loosens the threshold AFTER submission.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        row = (
            await session.execute(
                select(TenantApprovalSettings).where(
                    TenantApprovalSettings.tenant_id == tenant_id
                )
            )
        ).scalar_one()
        row.admin_required_above_amount = Decimal("99999.00")

    # Manager still gets the original-snapshot 403 because 8000 > 5000 was
    # captured at submission time.
    user = await _make_user(db_engine, tenant_id, UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/content-assets/{asset_id}/approve", json={}
            )
            assert resp.status_code == 403
            assert "admin" in resp.json()["detail"]["message"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Single approve gate
# ---------------------------------------------------------------------------


async def test_manager_can_approve_when_under_threshold(
    override_api_db, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_settings(db_engine, tenant_id, admin_above=Decimal("5000.00"))
    campaign_id = await _seed_campaign(
        db_engine, tenant_id, budget=Decimal("2000.00")
    )
    asset_id = await _seed_drafted_asset(db_engine, tenant_id, campaign_id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")

    user = await _make_user(db_engine, tenant_id, UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/content-assets/{asset_id}/approve", json={}
            )
            assert resp.status_code == 201, resp.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_manager_refused_above_threshold(
    override_api_db, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_settings(db_engine, tenant_id, admin_above=Decimal("5000.00"))
    campaign_id = await _seed_campaign(
        db_engine, tenant_id, budget=Decimal("8000.00")
    )
    asset_id = await _seed_drafted_asset(db_engine, tenant_id, campaign_id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")

    user = await _make_user(db_engine, tenant_id, UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/content-assets/{asset_id}/approve", json={}
            )
            assert resp.status_code == 403
            detail = resp.json()["detail"]
            assert detail["applied_threshold"]["admin_required_above_amount"] == "5000.00"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_admin_can_approve_above_threshold(
    override_api_db, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_settings(db_engine, tenant_id, admin_above=Decimal("5000.00"))
    campaign_id = await _seed_campaign(
        db_engine, tenant_id, budget=Decimal("8000.00")
    )
    asset_id = await _seed_drafted_asset(db_engine, tenant_id, campaign_id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")

    user = await _make_user(db_engine, tenant_id, UserRole.admin)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/content-assets/{asset_id}/approve", json={}
            )
            assert resp.status_code == 201, resp.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_approve_audit_records_applied_threshold(
    override_api_db, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_settings(db_engine, tenant_id, admin_above=Decimal("5000.00"))
    campaign_id = await _seed_campaign(
        db_engine, tenant_id, budget=Decimal("2000.00")
    )
    asset_id = await _seed_drafted_asset(db_engine, tenant_id, campaign_id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")

    user = await _make_user(db_engine, tenant_id, UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(f"/api/content-assets/{asset_id}/approve", json={})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "content_asset",
                    AuditLog.entity_id == asset_id,
                    AuditLog.action == "approved",
                )
            )
        ).scalars().all()
        assert audits
        threshold_meta = audits[0].extra_metadata["applied_threshold"]
        assert threshold_meta["admin_required_above_amount"] == "5000.00"
        assert threshold_meta["campaign_budget"] == "2000.00"


async def test_currency_mismatch_skips_threshold(
    override_api_db, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _set_settings(
        db_engine,
        tenant_id,
        admin_above=Decimal("5000.00"),
        currency="USD",
    )
    # Campaign in EUR — currency mismatch should skip the threshold.
    campaign_id = await _seed_campaign(
        db_engine, tenant_id, budget=Decimal("100000.00"), currency="EUR"
    )
    asset_id = await _seed_drafted_asset(db_engine, tenant_id, campaign_id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")

    user = await _make_user(db_engine, tenant_id, UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/content-assets/{asset_id}/approve", json={}
            )
            # Mismatch → threshold skipped → manager can approve.
            assert resp.status_code == 201
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "content_asset",
                    AuditLog.entity_id == asset_id,
                    AuditLog.action == "approved",
                )
            )
        ).scalars().all()
        assert audits[0].extra_metadata["applied_threshold"]["skipped"] == "currency_mismatch"
