"""W26 — Batch approval (E07-S03).

Covers the four AC + the per-asset exclusion shapes:
  * #1 dry_run returns summary (channel counts, total spend, would-approve count)
  * #2 per-asset atomicity (one failure doesn't cascade)
  * #3 compliance-blocked asset is excluded
  * #4 above auto_approval_cap → excluded
Plus the cross-cutting threshold gate (above admin threshold → excluded for
non-admin reviewer; admin sees the same batch include the row).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
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
    ApprovalDecisionLog,
    Audience,
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
        tenant = Tenant(name=f"batch-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        for platform, name in [
            (ChannelPlatform.email, "Email"),
            (ChannelPlatform.linkedin, "LinkedIn"),
        ]:
            session.add(
                Channel(tenant_id=tenant.id, name=name, platform=platform, is_active=True)
            )
        return tenant.id


async def _settings(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    admin_above: Decimal | None = None,
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


async def _campaign(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    budget: Decimal,
    currency: str = "USD",
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        owner = AppUser(
            tenant_id=tenant_id,
            email=f"owner-{uuid.uuid4().hex[:6]}@batch.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant_id,
            owner_id=owner.id,
            name=f"c-{uuid.uuid4().hex[:4]}",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=budget,
            currency=currency,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=CampaignStatus.content_in_production,
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


async def _drafted_asset(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    *,
    platform: str = "email",
    extra_metadata: dict | None = None,
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        base_metadata: dict = {"channel_platform": platform, "fields": {"subject": "s"}}
        if extra_metadata:
            base_metadata.update(extra_metadata)
        asset = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            asset_type=AssetType.email if platform == "email" else AssetType.social_post,
            status=AssetStatus.drafted,
            title="t",
            content="b",
            extra_metadata=base_metadata,
            is_required=True,
        )
        session.add(asset)
        await session.flush()
        return asset.id


async def _submit_campaign(db_engine: AsyncEngine, tenant_id, campaign_id) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await campaign_sm.apply(session, campaign, "submit_for_approval")


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@batch.test",
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
async def as_role(tenant_id, db_engine: AsyncEngine) -> AsyncIterator:
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
# Dry-run summary (E07-S03 #1)
# ---------------------------------------------------------------------------


async def test_dry_run_returns_summary_without_writing(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    await _settings(db_engine, tenant_id, auto_cap=Decimal("999999.00"))
    c1 = await _campaign(db_engine, tenant_id, budget=Decimal("5000.00"))
    c2 = await _campaign(db_engine, tenant_id, budget=Decimal("3000.00"))
    a1 = await _drafted_asset(db_engine, tenant_id, c1, platform="email")
    a2 = await _drafted_asset(db_engine, tenant_id, c1, platform="linkedin")
    a3 = await _drafted_asset(db_engine, tenant_id, c2, platform="email")
    await _submit_campaign(db_engine, tenant_id, c1)
    await _submit_campaign(db_engine, tenant_id, c2)

    client, _ = await as_role(UserRole.manager)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a1), str(a2), str(a3)], "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["summary"]["would_approve_count"] == 3
    assert body["summary"]["channel_counts"]["email"] == 2
    assert body["summary"]["channel_counts"]["linkedin"] == 1
    # Total spend is per-distinct-campaign so 5000 + 3000 = 8000.
    assert body["summary"]["total_spend_exposed"] == "8000.00"

    # No decision rows written for these specific assets (other tests share
    # the session-scoped DB, so we must scope by asset_id).
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(ApprovalDecisionLog).where(
                    ApprovalDecisionLog.content_asset_id.in_([a1, a2, a3])
                )
            )
        ).scalars().all()
        assert rows == []


# ---------------------------------------------------------------------------
# Actual approve + per-asset atomicity (E07-S03 #2)
# ---------------------------------------------------------------------------


async def test_batch_approve_writes_per_asset_decisions(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    await _settings(db_engine, tenant_id, auto_cap=Decimal("999999.00"))
    c = await _campaign(db_engine, tenant_id, budget=Decimal("2000.00"))
    a1 = await _drafted_asset(db_engine, tenant_id, c, platform="email")
    a2 = await _drafted_asset(db_engine, tenant_id, c, platform="linkedin")
    await _submit_campaign(db_engine, tenant_id, c)

    client, _ = await as_role(UserRole.manager)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a1), str(a2)], "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["approved"]) == 2
    assert body["excluded"] == []

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        assets = (
            await session.execute(
                select(ContentAsset).where(ContentAsset.campaign_id == c)
            )
        ).scalars().all()
        assert {a.status for a in assets} == {AssetStatus.approved}

        # Scope to this test's assets — the testcontainer is session-scoped.
        decisions = (
            await session.execute(
                select(ApprovalDecisionLog).where(
                    ApprovalDecisionLog.content_asset_id.in_([a1, a2])
                )
            )
        ).scalars().all()
        assert {str(d.content_asset_id) for d in decisions} == {str(a1), str(a2)}


async def test_batch_approve_continues_after_excluded_asset(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    """One asset in the batch is compliance-blocked; the rest still approve.

    A compliance-blocked required asset would normally block submit_for_approval
    (W23 guard), so the realistic scenario is "a compliance rule was added
    after the asset reached the queue, retroactively flagging it." We model
    that here: submit first, then flag.
    """
    await _settings(db_engine, tenant_id, auto_cap=Decimal("999999.00"))
    c = await _campaign(db_engine, tenant_id, budget=Decimal("2000.00"))
    a_clean = await _drafted_asset(db_engine, tenant_id, c, platform="email")
    a_blocked = await _drafted_asset(db_engine, tenant_id, c, platform="linkedin")
    await _submit_campaign(db_engine, tenant_id, c)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        row = await session.get(ContentAsset, a_blocked)
        row.extra_metadata = {
            **row.extra_metadata,
            "compliance": {
                "blocked": True,
                "hits": [{"rule": "x", "severity": "block"}],
            },
        }

    client, _ = await as_role(UserRole.manager)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a_clean), str(a_blocked)], "dry_run": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert {e["asset_id"] for e in body["approved"]} == {str(a_clean)}
    excluded_by_id = {e["asset_id"]: e for e in body["excluded"]}
    assert excluded_by_id[str(a_blocked)]["reason"] == "compliance_blocked"


# ---------------------------------------------------------------------------
# Auto-approval cap (E07-S03 #4)
# ---------------------------------------------------------------------------


async def test_above_auto_approval_cap_is_excluded(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    await _settings(db_engine, tenant_id, auto_cap=Decimal("1000.00"))
    big = await _campaign(db_engine, tenant_id, budget=Decimal("5000.00"))
    small = await _campaign(db_engine, tenant_id, budget=Decimal("500.00"))
    a_big = await _drafted_asset(db_engine, tenant_id, big, platform="email")
    a_small = await _drafted_asset(db_engine, tenant_id, small, platform="email")
    await _submit_campaign(db_engine, tenant_id, big)
    await _submit_campaign(db_engine, tenant_id, small)

    client, _ = await as_role(UserRole.manager)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a_big), str(a_small)], "dry_run": False},
    )
    body = resp.json()
    assert {e["asset_id"] for e in body["approved"]} == {str(a_small)}
    excluded = {e["asset_id"]: e for e in body["excluded"]}
    assert excluded[str(a_big)]["reason"] == "above_auto_approval_cap"
    assert excluded[str(a_big)]["details"]["cap"] == "1000.00"


async def test_default_cap_zero_excludes_every_paid_campaign(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    """With no settings row → cap defaults to 0 → every campaign with a
    positive budget is excluded from batch auto-approval."""
    c = await _campaign(db_engine, tenant_id, budget=Decimal("100.00"))
    a = await _drafted_asset(db_engine, tenant_id, c, platform="email")
    await _submit_campaign(db_engine, tenant_id, c)

    client, _ = await as_role(UserRole.manager)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a)], "dry_run": False},
    )
    body = resp.json()
    assert body["approved"] == []
    assert body["excluded"][0]["reason"] == "above_auto_approval_cap"


# ---------------------------------------------------------------------------
# Admin-required threshold within batch
# ---------------------------------------------------------------------------


async def test_manager_batch_excludes_admin_required_assets(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    await _settings(
        db_engine,
        tenant_id,
        admin_above=Decimal("3000.00"),
        auto_cap=Decimal("999999.00"),
    )
    low = await _campaign(db_engine, tenant_id, budget=Decimal("1000.00"))
    high = await _campaign(db_engine, tenant_id, budget=Decimal("5000.00"))
    a_low = await _drafted_asset(db_engine, tenant_id, low, platform="email")
    a_high = await _drafted_asset(db_engine, tenant_id, high, platform="email")
    await _submit_campaign(db_engine, tenant_id, low)
    await _submit_campaign(db_engine, tenant_id, high)

    client, _ = await as_role(UserRole.manager)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a_low), str(a_high)], "dry_run": False},
    )
    body = resp.json()
    assert {e["asset_id"] for e in body["approved"]} == {str(a_low)}
    excluded = {e["asset_id"]: e for e in body["excluded"]}
    assert excluded[str(a_high)]["reason"] == "requires_admin_role"


async def test_admin_batch_includes_admin_required_assets(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    await _settings(
        db_engine,
        tenant_id,
        admin_above=Decimal("3000.00"),
        auto_cap=Decimal("999999.00"),
    )
    high = await _campaign(db_engine, tenant_id, budget=Decimal("5000.00"))
    asset_id = await _drafted_asset(db_engine, tenant_id, high, platform="email")
    await _submit_campaign(db_engine, tenant_id, high)

    client, _ = await as_role(UserRole.admin)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(asset_id)], "dry_run": False},
    )
    body = resp.json()
    assert {e["asset_id"] for e in body["approved"]} == {str(asset_id)}


# ---------------------------------------------------------------------------
# Wrong-status + missing-asset paths
# ---------------------------------------------------------------------------


async def test_wrong_status_asset_excluded(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    await _settings(db_engine, tenant_id, auto_cap=Decimal("999999.00"))
    c = await _campaign(db_engine, tenant_id, budget=Decimal("100.00"))
    a = await _drafted_asset(db_engine, tenant_id, c, platform="email")
    # No submit — asset still in `drafted` not pending_approval.

    client, _ = await as_role(UserRole.manager)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a)], "dry_run": False},
    )
    body = resp.json()
    assert body["approved"] == []
    assert body["excluded"][0]["reason"] == "wrong_status"
    assert body["excluded"][0]["details"]["status"] == "drafted"


async def test_unknown_asset_id_returns_not_found_exclusion(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    client, _ = await as_role(UserRole.manager)
    bogus = uuid.uuid4()
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(bogus)], "dry_run": False},
    )
    body = resp.json()
    assert body["approved"] == []
    assert body["excluded"][0]["reason"] == "not_found"


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_batch_endpoint_requires_manager_role(
    db_engine: AsyncEngine, tenant_id, as_role, role
) -> None:
    client, _ = await as_role(role)
    resp = await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Forward state transition after batch approval
# ---------------------------------------------------------------------------


async def test_batch_drives_campaign_to_ready_to_launch(
    db_engine: AsyncEngine, tenant_id, as_role
) -> None:
    await _settings(db_engine, tenant_id, auto_cap=Decimal("999999.00"))
    c = await _campaign(db_engine, tenant_id, budget=Decimal("1000.00"))
    a1 = await _drafted_asset(db_engine, tenant_id, c, platform="email")
    a2 = await _drafted_asset(db_engine, tenant_id, c, platform="linkedin")
    await _submit_campaign(db_engine, tenant_id, c)

    client, _ = await as_role(UserRole.manager)
    await client.post(
        "/api/approvals/batch-approve",
        json={"asset_ids": [str(a1), str(a2)], "dry_run": False},
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, c)
        assert campaign.status == CampaignStatus.ready_to_launch
