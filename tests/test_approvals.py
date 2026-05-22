"""W25 — Approval queue + per-asset decisions (E07-S01, E07-S02).

Three layers under test:

  * Queue surface — ordering, filters, overdue flag, manager-only gate.
  * Approve / reject — log row, status flip, edit diff, campaign transitions
    forward (approve) and backward (reject).
  * State machine — submit_for_approval on_enter flips drafted assets to
    pending_approval; start_launch only fires when all required assets are
    approved; regenerate_after_rejection only fires when something is rejected.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    ApprovalDecision,
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
    AuditLog,
    Campaign,
    Channel,
    ContentAsset,
    StrategyProposal,
    StrategyTouchpoint,
    Task,
    Tenant,
)
from app.db.session import set_tenant_context
from app.orchestrator.state_machine import campaign_sm


# ---------------------------------------------------------------------------
# Helpers — DB seeding
# ---------------------------------------------------------------------------


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    campaign_state: CampaignStatus = CampaignStatus.approval_pending,
    asset_statuses: list[AssetStatus] | None = None,
    asset_metadata_overrides: list[dict] | None = None,
    submitted_offsets: list[timedelta] | None = None,
) -> dict[str, list[uuid.UUID] | uuid.UUID]:
    """Tenant + campaign + audience + channel + N content assets.

    Returns a dict with tenant_id, campaign_id, asset_ids list. The first
    asset is email, second is linkedin (so platform filters can be tested).
    """
    if asset_statuses is None:
        asset_statuses = [AssetStatus.pending_approval, AssetStatus.pending_approval]
    asset_metadata_overrides = asset_metadata_overrides or [{}, {}]
    submitted_offsets = submitted_offsets or [timedelta(hours=1), timedelta(hours=1)]

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"appr-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()

        for platform, name in [
            (ChannelPlatform.email, "Email"),
            (ChannelPlatform.linkedin, "LinkedIn"),
        ]:
            session.add(
                Channel(tenant_id=tenant.id, name=name, platform=platform, is_active=True)
            )

        owner = AppUser(
            tenant_id=tenant.id,
            email=f"owner-{uuid.uuid4().hex[:6]}@appr.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()

        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="appr-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=campaign_state,
        )
        session.add(campaign)
        await session.flush()

        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seg",
            segment_criteria={},
            estimated_size=10,
            actual_size=10,
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()

        asset_ids: list[uuid.UUID] = []
        now = datetime.now(UTC)
        for idx, (status, extra, offset) in enumerate(
            zip(asset_statuses, asset_metadata_overrides, submitted_offsets, strict=False)
        ):
            asset_type = (
                AssetType.email if idx == 0 else AssetType.social_post
            )
            platform_value = "email" if idx == 0 else "linkedin"
            base_metadata = {
                "channel_platform": platform_value,
                "fields": {
                    "subject": f"S{idx}",
                    "preheader": f"P{idx}",
                    "cta": f"CTA{idx}",
                },
            }
            base_metadata.update(extra)
            asset = ContentAsset(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                asset_type=asset_type,
                status=status,
                title=f"Asset {idx}",
                content=f"Body {idx}",
                extra_metadata=base_metadata,
                is_required=True,
                updated_at=now - offset,
            )
            session.add(asset)
            await session.flush()
            asset_ids.append(asset.id)

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "asset_ids": asset_ids,
            "owner_id": owner.id,
        }


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@appr.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def world(override_api_db, db_engine: AsyncEngine):
    return await _seed_world(db_engine)


@pytest.fixture
async def client_as(world, db_engine: AsyncEngine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, world["tenant_id"], role)
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
# Queue
# ---------------------------------------------------------------------------


async def test_queue_returns_pending_approval_assets(client_as, world) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.get("/api/approvals/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    asset_ids = {item["asset_id"] for item in body["items"]}
    assert asset_ids == {str(world["asset_ids"][0]), str(world["asset_ids"][1])}


async def test_queue_only_lists_pending_approval(
    override_api_db, db_engine: AsyncEngine
) -> None:
    world = await _seed_world(
        db_engine,
        asset_statuses=[AssetStatus.pending_approval, AssetStatus.drafted],
    )
    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/approvals/queue")
            assert resp.json()["total"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_queue_flags_overdue_after_24h(
    override_api_db, db_engine: AsyncEngine
) -> None:
    world = await _seed_world(
        db_engine,
        submitted_offsets=[timedelta(hours=30), timedelta(minutes=10)],
    )
    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = (await client.get("/api/approvals/queue")).json()
            overdue_by_id = {it["asset_id"]: it["overdue"] for it in body["items"]}
            assert overdue_by_id[str(world["asset_ids"][0])] is True
            assert overdue_by_id[str(world["asset_ids"][1])] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_queue_filters_by_channel_and_asset_type(client_as) -> None:
    client, _ = await client_as(UserRole.manager)
    by_channel = await client.get(
        "/api/approvals/queue", params={"channel_platform": "email"}
    )
    assert by_channel.json()["total"] == 1
    assert by_channel.json()["items"][0]["channel_platform"] == "email"

    by_type = await client.get(
        "/api/approvals/queue", params={"asset_type": "social_post"}
    )
    assert by_type.json()["total"] == 1
    assert by_type.json()["items"][0]["asset_type"] == "social_post"


async def test_queue_filters_by_submitter(client_as, world) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.get(
        "/api/approvals/queue",
        params={"submitter_id": str(world["owner_id"])},
    )
    assert resp.json()["total"] == 2

    other = uuid.uuid4()
    resp = await client.get("/api/approvals/queue", params={"submitter_id": str(other)})
    assert resp.json()["total"] == 0


async def test_queue_ordered_by_end_date_then_submitted_at(
    override_api_db, db_engine: AsyncEngine
) -> None:
    """Two campaigns; the one with the earlier end_date should come first."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"order-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        for platform, name in [
            (ChannelPlatform.email, "Email"),
        ]:
            session.add(
                Channel(tenant_id=tenant.id, name=name, platform=platform, is_active=True)
            )
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o@{uuid.uuid4().hex[:6]}.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()

        early_campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="early",
            campaign_type=CampaignType.awareness,
            objective="x",
            budget_total=Decimal("1000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 10),
            status=CampaignStatus.approval_pending,
        )
        late_campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="late",
            campaign_type=CampaignType.awareness,
            objective="x",
            budget_total=Decimal("1000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 7, 30),
            status=CampaignStatus.approval_pending,
        )
        session.add_all([early_campaign, late_campaign])
        await session.flush()

        for campaign in (early_campaign, late_campaign):
            session.add(
                ContentAsset(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    asset_type=AssetType.email,
                    status=AssetStatus.pending_approval,
                    title=f"{campaign.name}-asset",
                    content="x",
                    extra_metadata={"channel_platform": "email"},
                    is_required=True,
                )
            )
        await session.flush()
        tenant_id = tenant.id

    user = await _make_user(db_engine, tenant_id, UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            items = (await client.get("/api/approvals/queue")).json()["items"]
            assert [it["campaign_name"] for it in items] == ["early", "late"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_queue_rejects_non_manager(client_as, role) -> None:
    client, _ = await client_as(role)
    resp = await client.get("/api/approvals/queue")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


async def test_approve_writes_log_and_flips_status(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, manager = await client_as(UserRole.manager)
    asset_id = world["asset_ids"][0]
    resp = await client.post(
        f"/api/content-assets/{asset_id}/approve", json={}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["reviewer_id"] == str(manager.id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, asset_id)
        assert asset.status == AssetStatus.approved
        logs = (
            await session.execute(
                select(ApprovalDecisionLog).where(
                    ApprovalDecisionLog.content_asset_id == asset_id
                )
            )
        ).scalars().all()
        assert len(logs) == 1


async def test_approve_with_edits_stores_diff_and_updates_content(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.manager)
    asset_id = world["asset_ids"][0]
    resp = await client.post(
        f"/api/content-assets/{asset_id}/approve",
        json={
            "edited_content": "Edited body for asset 0",
            "edited_fields": {"subject": "New subject"},
            "note": "Tightened CTA",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["decision"] == "approved_with_edits"
    assert body["edits"]["previous_content"] == "Body 0"
    assert body["edits"]["current_content"] == "Edited body for asset 0"
    assert body["edits"]["current_fields"]["subject"] == "New subject"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, asset_id)
        assert asset.content == "Edited body for asset 0"
        assert asset.extra_metadata["fields"]["subject"] == "New subject"


async def test_approve_rejects_compliance_blocked_asset(
    override_api_db, db_engine: AsyncEngine
) -> None:
    world = await _seed_world(
        db_engine,
        asset_metadata_overrides=[
            {
                "compliance": {
                    "blocked": True,
                    "hits": [{"rule": "x", "severity": "block"}],
                }
            },
            {},
        ],
    )
    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/content-assets/{world['asset_ids'][0]}/approve", json={}
            )
            assert resp.status_code == 422
            assert "compliance" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_approve_rejects_wrong_status(client_as, world, db_engine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        asset.status = AssetStatus.drafted

    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/api/content-assets/{world['asset_ids'][0]}/approve", json={}
    )
    assert resp.status_code == 409


async def test_approve_writes_audit_log(client_as, world, db_engine) -> None:
    client, _ = await client_as(UserRole.manager)
    asset_id = world["asset_ids"][0]
    await client.post(f"/api/content-assets/{asset_id}/approve", json={})

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
        assert len(audits) == 1


# ---------------------------------------------------------------------------
# Approve → start_launch transition
# ---------------------------------------------------------------------------


async def test_last_approval_drives_campaign_to_ready_to_launch(
    client_as, world, db_engine
) -> None:
    client, _ = await client_as(UserRole.manager)
    # Approve both required assets in sequence.
    await client.post(
        f"/api/content-assets/{world['asset_ids'][0]}/approve", json={}
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        # Still pending — second asset is in pending_approval.
        assert campaign.status == CampaignStatus.approval_pending

    await client.post(
        f"/api/content-assets/{world['asset_ids'][1]}/approve", json={}
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        assert campaign.status == CampaignStatus.ready_to_launch


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


async def test_reject_writes_log_enqueues_task_and_flips_status(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, manager = await client_as(UserRole.manager)
    asset_id = world["asset_ids"][0]

    resp = await client.post(
        f"/api/content-assets/{asset_id}/reject",
        json={
            "reason": "Off-voice in opening paragraph",
            "category": "off_voice",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["decision"] == "rejected"
    assert body["reason"] == "Off-voice in opening paragraph"
    assert body["edits"]["category"] == "off_voice"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, asset_id)
        assert asset.status == AssetStatus.rejected

        tasks = (
            await session.execute(
                select(Task).where(
                    Task.campaign_id == world["campaign_id"],
                    Task.skill_name == "content_creator.generate_asset",
                )
            )
        ).scalars().all()
        assert any(
            t.input_data.get("rejection_reason") == "Off-voice in opening paragraph"
            for t in tasks
        )


async def test_reject_reverts_campaign_to_content_in_production(
    client_as, world, db_engine
) -> None:
    client, _ = await client_as(UserRole.manager)
    await client.post(
        f"/api/content-assets/{world['asset_ids'][0]}/reject",
        json={"reason": "needs work", "category": "other"},
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        assert campaign.status == CampaignStatus.content_in_production


async def test_reject_requires_reason(client_as, world) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/api/content-assets/{world['asset_ids'][0]}/reject", json={"reason": ""}
    )
    assert resp.status_code == 422


async def test_reject_rejects_wrong_status(client_as, world, db_engine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        asset.status = AssetStatus.drafted

    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/api/content-assets/{world['asset_ids'][0]}/reject",
        json={"reason": "x", "category": "other"},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_decisions_require_manager_role(client_as, world, role) -> None:
    client, _ = await client_as(role)
    asset_id = world["asset_ids"][0]
    assert (
        await client.post(f"/api/content-assets/{asset_id}/approve", json={})
    ).status_code == 403
    assert (
        await client.post(
            f"/api/content-assets/{asset_id}/reject",
            json={"reason": "x", "category": "other"},
        )
    ).status_code == 403


# ---------------------------------------------------------------------------
# Approval history
# ---------------------------------------------------------------------------


async def test_history_returns_decisions_newest_first(
    client_as, world, db_engine
) -> None:
    client, _ = await client_as(UserRole.manager)
    asset_id = world["asset_ids"][0]

    # Reject then approve a new draft (after the reject the asset is in
    # `rejected`; the worker would flip it back to `drafted` then back to
    # `pending_approval`. Skip the worker — flip it directly for the test).
    await client.post(
        f"/api/content-assets/{asset_id}/reject",
        json={"reason": "not punchy", "category": "off_voice"},
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = await session.get(ContentAsset, asset_id)
        asset.status = AssetStatus.pending_approval
        # Campaign was reverted; bring it back so the next approve can fire start_launch.
        campaign = await session.get(Campaign, world["campaign_id"])
        campaign.status = CampaignStatus.approval_pending

    await client.post(f"/api/content-assets/{asset_id}/approve", json={})

    resp = await client.get(
        f"/api/content-assets/{asset_id}/approval-history"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    decisions = [d["decision"] for d in body["decisions"]]
    # Newest first.
    assert decisions == ["approved", "rejected"]


# ---------------------------------------------------------------------------
# State machine: submit_for_approval on_enter
# ---------------------------------------------------------------------------


async def test_submit_for_approval_flips_drafted_assets_to_pending_approval(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(
        db_engine,
        campaign_state=CampaignStatus.content_in_production,
        asset_statuses=[AssetStatus.drafted, AssetStatus.drafted],
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await campaign_sm.apply(session, campaign, "submit_for_approval")
        assert campaign.status == CampaignStatus.approval_pending

        rows = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()
        assert all(r.status == AssetStatus.pending_approval for r in rows)


async def test_start_launch_blocked_when_any_asset_still_pending(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(
        db_engine,
        asset_statuses=[AssetStatus.approved, AssetStatus.pending_approval],
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        from app.orchestrator.state_machine import GuardFailedError

        with pytest.raises(GuardFailedError):
            await campaign_sm.apply(session, campaign, "start_launch")
