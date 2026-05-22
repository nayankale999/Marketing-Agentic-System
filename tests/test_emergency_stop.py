"""W31 — Emergency stop (E08-S07).

Coverage:
  * POST /pause flips status, cancels queued + awaiting_retry tasks,
    leaves running tasks alone.
  * Dispatching against a paused campaign returns campaign_paused early
    instead of touching the asset.
  * POST /resume re-enqueues future-slot assets, fails elapsed-slot ones.
  * State machine refuses pause from non-active states + resume from
    non-paused state.
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

from app.agents.distribution import (
    dispatch_email_asset,
    pause_campaign,
    resume_distribution_for_campaign,
)
from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    TaskStatus,
    UserRole,
)
from app.db.models import (
    AppUser,
    Audience,
    AudienceMember,
    AuditLog,
    Campaign,
    Channel,
    ContentAsset,
    IntegrationCredential,
    Task,
    Tenant,
)
from app.db.session import set_tenant_context
from app.integrations.credentials import get_encrypted_payload
from app.orchestrator.queue import enqueue_task
from app.orchestrator.state_machine import (
    GuardFailedError,
    UnknownTransitionError,
    campaign_sm,
)


async def _seed_email_credential(
    db_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    payload = {
        "api_key": "sg.test",
        "default_from_email": "alex@acme.com",
        "verified_senders": ["alex@acme.com"],
        "webhook_secret": "wh",
    }
    encrypted = get_encrypted_payload().encrypt(payload)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                channel_id=None,
                provider="sendgrid",
                label="default",
                encrypted_payload=encrypted,
                key_version=1,
            )
        )


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    campaign_state: CampaignStatus = CampaignStatus.live,
    queued_task_count: int = 2,
    asset_count: int = 2,
    scheduled_at_offsets_minutes: list[int] | None = None,
) -> dict[str, uuid.UUID | list[uuid.UUID]]:
    """Tenant + email channel + campaign + audience + N scheduled assets
    + N queued dispatch tasks. Returns the ids."""
    scheduled_at_offsets_minutes = scheduled_at_offsets_minutes or [60, 120]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"es-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        session.add(
            Channel(
                tenant_id=tenant.id,
                name="Email",
                platform=ChannelPlatform.email,
                is_active=True,
            )
        )
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@es.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        from app.agents.distribution import ensure_distribution_agent

        agent = await ensure_distribution_agent(session, tenant.id)

        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="es-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("100.00"),
            currency="USD",
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=30),
            status=campaign_state,
        )
        session.add(campaign)
        await session.flush()

        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seg",
            segment_criteria={},
            estimated_size=1,
            actual_size=1,
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()
        session.add(
            AudienceMember(
                audience_id=audience.id,
                external_id="m1",
                payload={"email": "m1@cust.com"},
                source="seed",
                fetched_at=datetime.now(UTC),
            )
        )

        asset_ids: list[uuid.UUID] = []
        for i in range(asset_count):
            offset = scheduled_at_offsets_minutes[
                min(i, len(scheduled_at_offsets_minutes) - 1)
            ]
            asset = ContentAsset(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                asset_type=AssetType.email,
                status=AssetStatus.scheduled,
                title=f"t{i}",
                content="body",
                extra_metadata={"channel_platform": "email", "fields": {"subject": "s"}},
                is_required=True,
                scheduled_at=datetime.now(UTC) + timedelta(minutes=offset),
            )
            session.add(asset)
            await session.flush()
            asset_ids.append(asset.id)

        # Queued tasks pointing at the assets.
        task_ids: list[uuid.UUID] = []
        for i in range(min(queued_task_count, asset_count)):
            task = await enqueue_task(
                session,
                tenant_id=tenant.id,
                agent_id=agent.id,
                campaign_id=campaign.id,
                skill_name="distribution.dispatch_email",
                input_data={
                    "asset_id": str(asset_ids[i]),
                    "campaign_id": str(campaign.id),
                },
                scheduled_for=datetime.now(UTC) + timedelta(minutes=scheduled_at_offsets_minutes[i] if i < len(scheduled_at_offsets_minutes) else 60),
            )
            task_ids.append(task.id)

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "asset_ids": asset_ids,
            "task_ids": task_ids,
        }


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@es.test",
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
async def client_as(world, db_engine) -> AsyncIterator:
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
# Pause
# ---------------------------------------------------------------------------


async def test_pause_flips_status_and_cancels_queued_tasks(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/api/campaigns/{world['campaign_id']}/pause",
        json={"reason": "manual"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "paused"
    assert body["cancelled_tasks"] == 2

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        assert campaign.status == CampaignStatus.paused
        tasks = (
            await session.execute(
                select(Task).where(Task.campaign_id == campaign.id)
            )
        ).scalars().all()
        assert all(t.status == TaskStatus.cancelled for t in tasks)


async def test_pause_leaves_running_tasks_alone(
    client_as, world, db_engine: AsyncEngine
) -> None:
    """A task in `running` state shouldn't be cancelled by pause — the AC
    says currently-executing batches finish."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        task = await session.get(Task, world["task_ids"][0])
        task.status = TaskStatus.running
        task.leased_until = datetime.now(UTC) + timedelta(seconds=30)
        task.worker_id = "worker-1"

    client, _ = await client_as(UserRole.manager)
    await client.post(f"/api/campaigns/{world['campaign_id']}/pause", json={})

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        running = await session.get(Task, world["task_ids"][0])
        other = await session.get(Task, world["task_ids"][1])
        assert running.status == TaskStatus.running  # untouched
        assert other.status == TaskStatus.cancelled


async def test_pause_writes_audit_with_reason(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.manager)
    await client.post(
        f"/api/campaigns/{world['campaign_id']}/pause",
        json={"reason": "spam_complaints_spiked"},
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "campaign",
                    AuditLog.entity_id == world["campaign_id"],
                )
            )
        ).scalars().all()
        # We expect both the SM transition audit AND the pause_reason follow-up.
        reasons = [
            a.extra_metadata.get("reason")
            for a in audits
            if a.extra_metadata and a.extra_metadata.get("reason")
        ]
        assert "spam_complaints_spiked" in reasons


async def test_pause_rejects_wrong_state(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(db_engine, campaign_state=CampaignStatus.drafted)
    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/campaigns/{world['campaign_id']}/pause", json={}
            )
            assert resp.status_code == 409
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_non_manager_cannot_pause(client_as, world, role) -> None:
    client, _ = await client_as(role)
    resp = await client.post(f"/api/campaigns/{world['campaign_id']}/pause", json={})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Dispatch while paused
# ---------------------------------------------------------------------------


async def test_dispatch_while_paused_returns_early_without_touching_asset(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(db_engine)
    await _seed_email_credential(db_engine, world["tenant_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await pause_campaign(session, campaign=campaign, reason="test")

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        out = await dispatch_email_asset(session, asset_id=world["asset_ids"][0])
        assert out["error"] == "campaign_paused"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        # Asset stayed in scheduled — dispatcher returned without flipping it.
        assert asset.status == AssetStatus.scheduled


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


async def test_resume_re_enqueues_future_slot_assets(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(
        db_engine,
        campaign_state=CampaignStatus.live,
        scheduled_at_offsets_minutes=[60, 120],  # both future
    )
    # Pause first to set up.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await pause_campaign(session, campaign=campaign, reason="test")

    # Verify all tasks are cancelled now.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tasks = (
            await session.execute(
                select(Task).where(Task.campaign_id == world["campaign_id"])
            )
        ).scalars().all()
        assert all(t.status == TaskStatus.cancelled for t in tasks)

    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/campaigns/{world['campaign_id']}/resume"
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "live"
            assert body["requeued"] == 2
            assert body["elapsed_failed"] == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # Confirm new queued tasks were created.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tasks = (
            await session.execute(
                select(Task).where(
                    Task.campaign_id == world["campaign_id"],
                    Task.status == TaskStatus.queued,
                )
            )
        ).scalars().all()
        assert len(tasks) == 2


async def test_resume_flips_elapsed_slot_assets_to_failed(
    db_engine: AsyncEngine, override_api_db
) -> None:
    """E08-S07 #3: assets whose slot has elapsed during pause get skipped."""
    world = await _seed_world(
        db_engine,
        campaign_state=CampaignStatus.live,
        scheduled_at_offsets_minutes=[60, 120],
    )
    # Pause.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await pause_campaign(session, campaign=campaign, reason="test")

    # Manually set one asset's slot into the past (simulates time passing).
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        asset.scheduled_at = datetime.now(UTC) - timedelta(hours=2)

    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/campaigns/{world['campaign_id']}/resume"
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["requeued"] == 1
            assert body["elapsed_failed"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        elapsed_asset = await session.get(ContentAsset, world["asset_ids"][0])
        future_asset = await session.get(ContentAsset, world["asset_ids"][1])
        assert elapsed_asset.status == AssetStatus.failed
        assert (
            elapsed_asset.extra_metadata.get("skip_reason")
            == "slot_elapsed_during_pause"
        )
        assert future_asset.status == AssetStatus.scheduled


async def test_resume_rejects_non_paused_campaign(client_as, world) -> None:
    """Campaign must be in `paused` to resume."""
    client, _ = await client_as(UserRole.manager)
    # world's campaign is live, not paused.
    resp = await client.post(f"/api/campaigns/{world['campaign_id']}/resume")
    assert resp.status_code == 409


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_non_manager_cannot_resume(client_as, world, role) -> None:
    # Role check fires before state check, so live-not-paused is irrelevant.
    client, _ = await client_as(role)
    resp = await client.post(f"/api/campaigns/{world['campaign_id']}/resume")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------


async def test_pause_transition_works_from_optimising(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(db_engine, campaign_state=CampaignStatus.optimising)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await campaign_sm.apply(session, campaign, "pause")
        assert campaign.status == CampaignStatus.paused


async def test_resume_transition_drives_paused_to_live(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(db_engine, campaign_state=CampaignStatus.paused)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await campaign_sm.apply(session, campaign, "resume")
        assert campaign.status == CampaignStatus.live


async def test_pause_helper_is_idempotent(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine, campaign_state=CampaignStatus.paused)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        # Already paused — second call is a no-op.
        await pause_campaign(session, campaign=campaign, reason="redundant")
        assert campaign.status == CampaignStatus.paused
