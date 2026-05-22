"""W29 — Frequency capping (E08-S04 #2 + admin CRUD).

Two layers:
  * /api/frequency-caps CRUD — admin GET/PUT/DELETE per channel.
  * Dispatch enforcement — when a recipient already has >= cap sends in
    the window, the dispatcher writes a `skipped` row instead of sending.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents.distribution import dispatch_email_asset, schedule_approved_assets
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
    Campaign,
    Channel,
    ContentAsset,
    DispatchAttempt,
    FrequencyCapSetting,
    IntegrationCredential,
    StrategyProposal,
    StrategyTouchpoint,
    Tenant,
)
from app.db.session import set_tenant_context
from app.integrations.credentials import get_encrypted_payload


_SENDGRID_API = "https://api.sendgrid.com/v3/mail/send"


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
    audience_emails: list[str] | None = None,
) -> dict[str, uuid.UUID | list[uuid.UUID]]:
    audience_emails = audience_emails or ["a@customer.com", "b@customer.com"]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"fc-{uuid.uuid4().hex[:6]}")
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
            email=f"o-{uuid.uuid4().hex[:6]}@fc.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()

        start_date = date.today() - timedelta(days=1)
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="fc-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("100.00"),
            currency="USD",
            start_date=start_date,
            end_date=start_date + timedelta(days=30),
            status=CampaignStatus.approval_pending,
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
                    {"platform": "email", "name": "Email", "allocation_pct": 100,
                     "allocation_amount": "100.00", "rationale": "x", "human_override": False}
                ],
                "kpis": {"primary": {"metric": "mql", "target": 100, "rationale": "z"}, "secondary": []},
            },
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
            scheduled_at=datetime.combine(start_date + timedelta(days=1), time(9, 0), UTC),
        )
        session.add(tp)
        await session.flush()

        asset = ContentAsset(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            asset_type=AssetType.email,
            status=AssetStatus.approved,
            title="t",
            content="Hi {first_name}, here's update.",
            extra_metadata={
                "channel_platform": "email",
                "touchpoint_id": str(tp.id),
                "fields": {"subject": "s"},
            },
            is_required=True,
        )
        session.add(asset)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "asset_ids": [asset.id],
        }


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@fc.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
async def tenant_id(override_api_db, db_engine: AsyncEngine) -> uuid.UUID:
    world = await _seed_world(db_engine)
    return world["tenant_id"]


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


async def test_admin_can_put_then_get_cap(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.put(
        "/api/frequency-caps/email",
        json={"max_sends_per_recipient": 2, "window_days": 7, "enabled": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel_platform"] == "email"
    assert body["max_sends_per_recipient"] == 2
    assert body["enabled"] is True

    fetched = await client.get("/api/frequency-caps/email")
    assert fetched.status_code == 200
    assert fetched.json()["max_sends_per_recipient"] == 2


async def test_put_upserts_existing_cap(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    body = {"max_sends_per_recipient": 3, "window_days": 7, "enabled": True}
    r1 = await client.put("/api/frequency-caps/email", json=body)
    r2 = await client.put(
        "/api/frequency-caps/email",
        json={**body, "max_sends_per_recipient": 5},
    )
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["max_sends_per_recipient"] == 5


async def test_list_returns_configured_caps(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    await client.put(
        "/api/frequency-caps/email",
        json={"max_sends_per_recipient": 3, "window_days": 7, "enabled": True},
    )
    await client.put(
        "/api/frequency-caps/linkedin",
        json={"max_sends_per_recipient": 1, "window_days": 30, "enabled": False},
    )
    resp = await client.get("/api/frequency-caps")
    assert resp.json()["total"] == 2


async def test_get_unconfigured_cap_returns_404(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.get("/api/frequency-caps/email")
    assert resp.status_code == 404


async def test_delete_cap(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    await client.put(
        "/api/frequency-caps/email",
        json={"max_sends_per_recipient": 3, "window_days": 7, "enabled": True},
    )
    resp = await client.delete("/api/frequency-caps/email")
    assert resp.status_code == 204
    assert (await client.get("/api/frequency-caps/email")).status_code == 404


@pytest.mark.parametrize("role", [UserRole.manager, UserRole.marketer, UserRole.viewer])
async def test_non_admin_cannot_manage_caps(client_as, role) -> None:
    client, _ = await client_as(role)
    assert (await client.get("/api/frequency-caps")).status_code == 403
    assert (
        await client.put(
            "/api/frequency-caps/email",
            json={"max_sends_per_recipient": 1, "window_days": 7, "enabled": True},
        )
    ).status_code == 403


# ---------------------------------------------------------------------------
# Dispatch enforcement
# ---------------------------------------------------------------------------


@respx.mock
async def test_cap_disabled_allows_send(db_engine: AsyncEngine) -> None:
    """`enabled=False` means the dispatcher ignores the cap."""
    world = await _seed_world(db_engine)
    await _seed_email_credential(db_engine, world["tenant_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            FrequencyCapSetting(
                tenant_id=world["tenant_id"],
                channel_platform=ChannelPlatform.email,
                max_sends_per_recipient=1,
                window_days=7,
                enabled=False,
            )
        )

    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        result = await dispatch_email_asset(session, asset_id=world["asset_ids"][0])
    assert result["summary"]["sent"] == 2
    assert result["summary"]["skipped"] == 0


@respx.mock
async def test_cap_enabled_skips_recipient_over_limit(db_engine: AsyncEngine) -> None:
    """E08-S04 #2: a recipient that has hit the cap gets a `skipped` row,
    not a send. Other recipients still send."""
    world = await _seed_world(db_engine)
    await _seed_email_credential(db_engine, world["tenant_id"])

    # Cap email at 1 send / 7 days, enabled. Pre-seed a prior send to
    # a@customer.com pointing at a DIFFERENT content_asset (a previous
    # campaign step) — same-asset prior sends would be caught by W28's
    # idempotency dedup first.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            FrequencyCapSetting(
                tenant_id=world["tenant_id"],
                channel_platform=ChannelPlatform.email,
                max_sends_per_recipient=1,
                window_days=7,
                enabled=True,
            )
        )
        prior_asset = ContentAsset(
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            asset_type=AssetType.email,
            status=AssetStatus.published,
            title="prior",
            content="prior",
            extra_metadata={"channel_platform": "email"},
            is_required=False,
        )
        session.add(prior_asset)
        await session.flush()
        session.add(
            DispatchAttempt(
                tenant_id=world["tenant_id"],
                content_asset_id=prior_asset.id,
                recipient_identifier="a@customer.com",
                idempotency_key=f"asset:{prior_asset.id}:recipient:a@customer.com",
                provider="sendgrid",
                provider_message_id="prev",
                status="sent",
                sent_at=datetime.now(UTC) - timedelta(days=2),
            )
        )

    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        result = await dispatch_email_asset(session, asset_id=world["asset_ids"][0])

    # The 24h-window safety net might also catch a@customer.com via the
    # prior send less than 24h... but we set it 2 days ago. So 24h-window
    # doesn't catch; the cap does.
    assert result["summary"]["skipped"] == 1
    assert result["summary"]["sent"] == 1

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        skipped = (
            await session.execute(
                select(DispatchAttempt).where(
                    DispatchAttempt.content_asset_id == world["asset_ids"][0],
                    DispatchAttempt.status == "skipped",
                )
            )
        ).scalars().all()
        assert len(skipped) == 1
        assert skipped[0].recipient_identifier == "a@customer.com"
        assert skipped[0].last_error == "frequency_cap"


@respx.mock
async def test_all_skipped_publishes_with_skip_reason(db_engine: AsyncEngine) -> None:
    """E08-S04 #4: when every recipient is capped, the asset still
    publishes — with sent=0 and skip_reason recorded."""
    world = await _seed_world(db_engine)
    await _seed_email_credential(db_engine, world["tenant_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            FrequencyCapSetting(
                tenant_id=world["tenant_id"],
                channel_platform=ChannelPlatform.email,
                max_sends_per_recipient=1,
                window_days=7,
                enabled=True,
            )
        )
        # A prior asset (different from the target) carries the at-cap sends.
        prior_asset = ContentAsset(
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            asset_type=AssetType.email,
            status=AssetStatus.published,
            title="prior-all",
            content="prior",
            extra_metadata={"channel_platform": "email"},
            is_required=False,
        )
        session.add(prior_asset)
        await session.flush()
        for email in ("a@customer.com", "b@customer.com"):
            session.add(
                DispatchAttempt(
                    tenant_id=world["tenant_id"],
                    content_asset_id=prior_asset.id,
                    recipient_identifier=email,
                    idempotency_key=f"asset:{prior_asset.id}:recipient:{email}",
                    provider="sendgrid",
                    provider_message_id="prev",
                    status="sent",
                    sent_at=datetime.now(UTC) - timedelta(days=3),
                )
            )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        result = await dispatch_email_asset(session, asset_id=world["asset_ids"][0])

    assert result["summary"]["sent"] == 0
    assert result["summary"]["skipped"] == 2
    assert result["summary"]["skip_reason"] == "frequency_cap"
    # Asset still flips to `published` — distribution doesn't block on caps.
    assert result["status"] == "published"
