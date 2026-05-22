"""W28 — Channel Distribution agent (E08-S01, E08-S02, E08-S05).

Three layers under test:

  * Scheduling — start_launch on_enter pulls slot from touchpoint, flips
    asset to `scheduled`, enqueues dispatch task, writes audit row with
    prior/new slot.
  * Dispatch — happy path: provider receives batch, asset → `published`,
    one dispatch_attempt row per recipient with provider_message_id, audit
    row, summary in metadata.
  * Idempotency (E08-S05) — retry of same task skips already-sent
    recipients; 24h-window dedup catches the safety-net case.
  * State transition — start_live fires when all required assets are
    scheduled/published and start_date is reached.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents.distribution import (
    DistributionPreconditionError,
    dispatch_email_asset,
    schedule_approved_assets,
)
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
    AudienceMember,
    AuditLog,
    Campaign,
    Channel,
    ContentAsset,
    DispatchAttempt,
    IntegrationCredential,
    StrategyProposal,
    StrategyTouchpoint,
    Task,
    Tenant,
)
from app.db.session import set_tenant_context
from app.integrations.credentials import get_encrypted_payload
from app.orchestrator.state_machine import GuardFailedError, campaign_sm


_SENDGRID_API = "https://api.sendgrid.com/v3/mail/send"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_email_credential(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    verified_senders: list[str] | None = None,
) -> None:
    payload = {
        "api_key": "sg.test-key-9999",
        "default_from_email": "alex@acme.com",
        "verified_senders": verified_senders or ["alex@acme.com"],
        "webhook_secret": "wh-secret",
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
    campaign_state: CampaignStatus = CampaignStatus.approval_pending,
    start_date_offset_days: int = 0,
    audience_emails: list[str] | None = None,
    asset_count: int = 1,
    skip_touchpoints_for: int = 0,
) -> dict[str, uuid.UUID | list[uuid.UUID]]:
    """Tenant + email channel + campaign + audience + N approved email assets,
    each linked to a touchpoint scheduled tomorrow (by default)."""
    audience_emails = audience_emails or ["a@customer.com", "b@customer.com"]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"dist-{uuid.uuid4().hex[:6]}")
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
            email=f"o-{uuid.uuid4().hex[:6]}@dist.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()

        start_date = date.today() + timedelta(days=start_date_offset_days)
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="dist-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("1000.00"),
            currency="USD",
            start_date=start_date,
            end_date=start_date + timedelta(days=30),
            status=campaign_state,
        )
        session.add(campaign)
        await session.flush()

        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seg",
            segment_criteria={},
            estimated_size=len(audience_emails),
            actual_size=len(audience_emails),
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()
        for em in audience_emails:
            session.add(
                AudienceMember(
                    audience_id=audience.id,
                    external_id=em,
                    payload={"email": em, "first_name": em.split("@", 1)[0]},
                    source="seed",
                    fetched_at=datetime.now(UTC),
                )
            )

        proposal = StrategyProposal(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            version=1,
            payload={
                "channels": [
                    {
                        "platform": "email",
                        "name": "Email",
                        "allocation_pct": 100,
                        "allocation_amount": "1000.00",
                        "rationale": "x",
                        "human_override": False,
                    }
                ],
                "kpis": {
                    "primary": {"metric": "mql", "target": 100, "rationale": "z"},
                    "secondary": [],
                },
            },
            is_accepted=True,
            created_by_kind="agent",
        )
        session.add(proposal)
        await session.flush()

        asset_ids: list[uuid.UUID] = []
        for i in range(asset_count):
            extra_metadata: dict = {
                "channel_platform": "email",
                "fields": {"subject": f"Subject {i}", "preheader": "Pre"},
            }
            if i >= skip_touchpoints_for:
                tp = StrategyTouchpoint(
                    tenant_id=tenant.id,
                    proposal_id=proposal.id,
                    channel_platform="email",
                    audience_id=audience.id,
                    scheduled_at=datetime.combine(
                        start_date + timedelta(days=1 + i),
                        time(9, 0),
                        UTC,
                    ),
                )
                session.add(tp)
                await session.flush()
                extra_metadata["touchpoint_id"] = str(tp.id)
            asset = ContentAsset(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                asset_type=AssetType.email,
                status=AssetStatus.approved,
                title=f"Asset {i}",
                content=f"Hi {{first_name}}, here's update {i}.",
                extra_metadata=extra_metadata,
                is_required=True,
            )
            session.add(asset)
            await session.flush()
            asset_ids.append(asset.id)

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "asset_ids": asset_ids,
        }


def _anthropic_unused_response() -> httpx.Response:
    """Placeholder for tests that don't go through the LLM — should never fire."""
    return httpx.Response(500, text="should not be called")


# ---------------------------------------------------------------------------
# Scheduling (E08-S01)
# ---------------------------------------------------------------------------


async def test_schedule_approved_assets_flips_status_and_enqueues_task(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_count=2)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        scheduled = await schedule_approved_assets(session, campaign=campaign)
    assert {a.id for a in scheduled} == set(world["asset_ids"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.id.in_(world["asset_ids"])
                )
            )
        ).scalars().all()
        assert all(r.status == AssetStatus.scheduled for r in rows)
        assert all(r.scheduled_at is not None for r in rows)

        tasks = (
            await session.execute(
                select(Task).where(
                    Task.campaign_id == world["campaign_id"],
                    Task.skill_name == "distribution.dispatch_email",
                )
            )
        ).scalars().all()
        assert len(tasks) == 2
        asset_ids_in_tasks = {
            uuid.UUID(t.input_data["asset_id"]) for t in tasks
        }
        assert asset_ids_in_tasks == set(world["asset_ids"])


async def test_schedule_writes_audit_with_prior_and_new_slot(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_count=1)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "content_asset",
                    AuditLog.entity_id == world["asset_ids"][0],
                    AuditLog.action == "scheduled",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        meta = audits[0].extra_metadata
        assert meta["prior_slot"] is None
        assert "new_slot" in meta and meta["new_slot"]


async def test_schedule_past_slot_defaults_to_send_now_and_warns(
    db_engine: AsyncEngine,
) -> None:
    # Touchpoint in the past — agent should still schedule, with a warning
    # in metadata that the UI will surface later.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"past-{uuid.uuid4().hex[:6]}")
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
        campaign = Campaign(
            tenant_id=tenant.id,
            name="past",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("100.00"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
            status=CampaignStatus.ready_to_launch,
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
        proposal = StrategyProposal(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            version=1,
            payload={"channels": [], "kpis": {"primary": {"metric": "x", "target": 1, "rationale": "z"}, "secondary": []}},
            is_accepted=True,
            created_by_kind="agent",
        )
        session.add(proposal)
        await session.flush()
        tp = StrategyTouchpoint(
            tenant_id=tenant.id,
            proposal_id=proposal.id,
            channel_platform="email",
            audience_id=audience.id,
            scheduled_at=datetime.now(UTC) - timedelta(hours=2),  # past
        )
        session.add(tp)
        await session.flush()
        asset = ContentAsset(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            asset_type=AssetType.email,
            status=AssetStatus.approved,
            title="x",
            content="x",
            extra_metadata={
                "channel_platform": "email",
                "touchpoint_id": str(tp.id),
                "fields": {"subject": "s"},
            },
            is_required=True,
        )
        session.add(asset)
        await session.flush()
        tenant_id = tenant.id
        asset_id = asset.id

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, asset.campaign_id)
        await schedule_approved_assets(session, campaign=campaign)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        row = await session.get(ContentAsset, asset_id)
        assert row.status == AssetStatus.scheduled
        warning = row.extra_metadata.get("schedule_warning")
        assert warning and warning["kind"] == "past_slot"


# ---------------------------------------------------------------------------
# Dispatch happy path (E08-S02)
# ---------------------------------------------------------------------------


@respx.mock
async def test_dispatch_email_asset_marks_published_and_writes_attempts(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_count=1)
    await _seed_email_credential(db_engine, world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-batch"})
    )

    # Pre-schedule the asset (mimics the start_launch on_enter path).
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    asset_id = world["asset_ids"][0]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        result = await dispatch_email_asset(session, asset_id=asset_id)

    assert result["status"] == "published"
    assert result["summary"]["sent"] == 2
    assert result["summary"]["audience_size"] == 2

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, asset_id)
        assert asset.status == AssetStatus.published
        assert asset.published_at is not None
        assert asset.extra_metadata["dispatch_summary"]["sent"] == 2
        attempts = (
            await session.execute(
                select(DispatchAttempt).where(
                    DispatchAttempt.content_asset_id == asset_id
                )
            )
        ).scalars().all()
        assert len(attempts) == 2
        assert {a.status for a in attempts} == {"sent"}
        # Provider message ids populated from the SendGrid X-Message-Id header.
        assert all(a.provider_message_id and a.provider_message_id.startswith("msg-batch.") for a in attempts)


@respx.mock
async def test_dispatch_skips_suppressed_recipients(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine, asset_count=1)
    await _seed_email_credential(db_engine, world["tenant_id"])
    # Seed a suppression for one of the audience members.
    from app.db.models import SuppressionEntry

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            SuppressionEntry(
                tenant_id=world["tenant_id"],
                channel_platform=ChannelPlatform.email,
                identifier="a@customer.com",
                reason="bounce",
            )
        )

    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-z"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        result = await dispatch_email_asset(session, asset_id=world["asset_ids"][0])

    assert result["summary"]["sent"] == 1
    assert result["summary"]["suppressed"] == 1


# ---------------------------------------------------------------------------
# Idempotency (E08-S05)
# ---------------------------------------------------------------------------


@respx.mock
async def test_retry_does_not_double_send(db_engine: AsyncEngine) -> None:
    """E08-S05 #1: a retry of the same dispatch task hits the idempotency_key
    skip and doesn't call the provider for already-sent recipients."""
    world = await _seed_world(db_engine, asset_count=1)
    await _seed_email_credential(db_engine, world["tenant_id"])
    route = respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-a"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        await dispatch_email_asset(session, asset_id=world["asset_ids"][0])

    first_call_count = route.call_count

    # Flip asset back to scheduled so the dispatch can run again (matches
    # what the queue retry path would do — same skill, same input_data).
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        asset.status = AssetStatus.scheduled

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        result = await dispatch_email_asset(session, asset_id=world["asset_ids"][0])

    # No new sends — every recipient already had a `sent` attempt.
    assert result["summary"]["sent"] == 0
    assert result["summary"]["deduped"] == 2
    # The provider should NOT have been called a second time.
    assert route.call_count == first_call_count

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        attempts = (
            await session.execute(
                select(DispatchAttempt).where(
                    DispatchAttempt.content_asset_id == world["asset_ids"][0]
                )
            )
        ).scalars().all()
        # Still exactly one row per recipient (no duplicates).
        assert len(attempts) == 2


@respx.mock
async def test_24h_window_dedup_safety_net(db_engine: AsyncEngine) -> None:
    """E08-S05 #3: a sent attempt within the last 24h to the same recipient
    blocks a second send even if the idempotency_key for THIS asset would
    not match (covers the 'sent-but-DB-write-crashed' scenario across
    different assets in the same step)."""
    world = await _seed_world(db_engine, asset_count=2)
    await _seed_email_credential(db_engine, world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-h"})
    )

    asset_a, asset_b = world["asset_ids"]
    # Pre-seed a `sent` attempt for asset_a → a@customer.com, an hour ago.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            DispatchAttempt(
                tenant_id=world["tenant_id"],
                content_asset_id=asset_a,
                recipient_identifier="a@customer.com",
                idempotency_key=f"asset:{asset_a}:recipient:a@customer.com",
                provider="sendgrid",
                provider_message_id="msg-prev",
                status="sent",
                sent_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        # Dispatch asset_b — different asset, but a@customer.com was sent
        # less than 24h ago via asset_a → should be deduped.
        result = await dispatch_email_asset(session, asset_id=asset_b)

    deduped_count = result["summary"]["deduped"]
    assert deduped_count >= 1
    # b@customer.com still got sent (no prior).
    assert result["summary"]["sent"] == 1


# ---------------------------------------------------------------------------
# Precondition + error paths
# ---------------------------------------------------------------------------


async def test_dispatch_raises_when_no_email_integration(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_count=1)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        with pytest.raises(DistributionPreconditionError) as exc:
            await dispatch_email_asset(session, asset_id=world["asset_ids"][0])
        assert "email integration" in str(exc.value).lower()


async def test_dispatch_raises_when_wrong_asset_status(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_count=1)
    await _seed_email_credential(db_engine, world["tenant_id"])
    # Asset is still `approved`, not `scheduled` — dispatcher refuses.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, world["tenant_id"])
        with pytest.raises(DistributionPreconditionError):
            await dispatch_email_asset(session, asset_id=world["asset_ids"][0])


# ---------------------------------------------------------------------------
# State machine: start_launch on_enter + start_live guard
# ---------------------------------------------------------------------------


async def test_start_launch_on_enter_schedules_assets(db_engine: AsyncEngine) -> None:
    """The full state-machine path: campaign in approval_pending with all
    assets approved → start_launch fires on_enter → assets are scheduled
    and tasks are enqueued."""
    world = await _seed_world(db_engine, asset_count=1)
    # Flip the asset to approved (start_launch guard requires it).
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for aid in world["asset_ids"]:
            asset = await session.get(ContentAsset, aid)
            asset.status = AssetStatus.approved

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await campaign_sm.apply(session, campaign, "start_launch")
        assert campaign.status == CampaignStatus.ready_to_launch

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(ContentAsset).where(ContentAsset.id.in_(world["asset_ids"]))
            )
        ).scalars().all()
        assert all(r.status == AssetStatus.scheduled for r in rows)


async def test_start_live_guard_blocks_when_start_date_in_future(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(
        db_engine,
        campaign_state=CampaignStatus.ready_to_launch,
        start_date_offset_days=7,  # future
        asset_count=1,
    )
    # Mark asset as scheduled (skipping the launch path).
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for aid in world["asset_ids"]:
            asset = await session.get(ContentAsset, aid)
            asset.status = AssetStatus.scheduled
            asset.scheduled_at = datetime.now(UTC) + timedelta(days=7)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        with pytest.raises(GuardFailedError):
            await campaign_sm.apply(session, campaign, "start_live")


@respx.mock
async def test_dispatch_advances_campaign_to_live(db_engine: AsyncEngine) -> None:
    """After the last asset publishes AND start_date is reached, the
    campaign auto-advances ready_to_launch → live."""
    world = await _seed_world(
        db_engine,
        campaign_state=CampaignStatus.approval_pending,
        start_date_offset_days=-1,  # start_date in the past so guard passes
        asset_count=1,
    )
    await _seed_email_credential(db_engine, world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-x"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await campaign_sm.apply(session, campaign, "start_launch")

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        await dispatch_email_asset(session, asset_id=world["asset_ids"][0])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        assert campaign.status == CampaignStatus.live


# ---------------------------------------------------------------------------
# API audit surface
# ---------------------------------------------------------------------------


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@dist-api.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def api_world(override_api_db, db_engine: AsyncEngine):
    return await _seed_world(db_engine, asset_count=1)


@pytest.fixture
async def client_as(api_world, db_engine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, api_world["tenant_id"], role)
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


@respx.mock
async def test_list_dispatch_attempts(
    client_as, api_world, db_engine: AsyncEngine
) -> None:
    await _seed_email_credential(db_engine, api_world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-k"})
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, api_world["tenant_id"])
        campaign = await session.get(Campaign, api_world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        await dispatch_email_asset(session, asset_id=api_world["asset_ids"][0])

    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(
        f"/api/campaigns/{api_world['campaign_id']}/dispatch-attempts"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {item["status"] for item in body["items"]} == {"sent"}


@respx.mock
async def test_filter_dispatch_attempts_by_status(
    client_as, api_world, db_engine: AsyncEngine
) -> None:
    await _seed_email_credential(db_engine, api_world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-k"})
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, api_world["tenant_id"])
        campaign = await session.get(Campaign, api_world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        await dispatch_email_asset(session, asset_id=api_world["asset_ids"][0])

    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(
        f"/api/campaigns/{api_world['campaign_id']}/dispatch-attempts",
        params={"status": "rejected"},
    )
    assert resp.json()["total"] == 0


async def test_get_dispatch_attempt_detail(
    client_as, api_world, db_engine: AsyncEngine
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        attempt = DispatchAttempt(
            tenant_id=api_world["tenant_id"],
            content_asset_id=api_world["asset_ids"][0],
            recipient_identifier="lookup@test.com",
            idempotency_key=f"asset:{api_world['asset_ids'][0]}:recipient:lookup@test.com",
            provider="sendgrid",
            status="sent",
            sent_at=datetime.now(UTC),
        )
        session.add(attempt)
        await session.flush()
        attempt_id = attempt.id

    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/dispatch-attempts/{attempt_id}")
    assert resp.status_code == 200
    assert resp.json()["recipient_identifier"] == "lookup@test.com"


async def test_get_dispatch_attempt_404(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/dispatch-attempts/{uuid.uuid4()}")
    assert resp.status_code == 404
