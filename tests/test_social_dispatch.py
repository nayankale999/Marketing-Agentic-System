"""W30 — End-to-end social dispatch (E08-S02 + E12-S03 + E11-S05).

Covers the Distribution agent's `dispatch_social_asset` flow:
  * happy path: scheduled social_post → published, dispatch_attempt row,
    provider_post_id captured, idempotency snapshot in metadata
  * retry idempotency: re-running the same task hits the dispatch_attempt
    cache instead of double-posting
  * OAuthRevokedError → campaign paused, audit row, exception propagates
  * `schedule_approved_assets` routes social_post → distribution.dispatch_social
    (not dispatch_email)
  * unmapped asset types stay scheduled but no task is enqueued
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents.distribution import (
    dispatch_social_asset,
    schedule_approved_assets,
)
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
from app.integrations.social.linkedin import LinkedInConnector


_UGC_URL = LinkedInConnector.UGC_POSTS_URL


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


async def _seed_linkedin_credential_and_channel(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    page_urn: str = "urn:li:organization:111",
) -> uuid.UUID:
    """Create a Channel + attached IntegrationCredential for LinkedIn."""
    payload = {
        "access_token": "tok-1",
        "refresh_token": "tok-R",
        "scopes": ["w_organization_social"],
    }
    encrypted = get_encrypted_payload().encrypt(payload)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        channel = Channel(
            tenant_id=tenant_id,
            name="Acme LinkedIn",
            platform=ChannelPlatform.linkedin,
            api_config={
                "provider": "linkedin",
                "page_id": "111",
                "page_urn": page_urn,
                "page_name": "Acme",
            },
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        session.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                channel_id=channel.id,
                provider="linkedin",
                label="111",
                encrypted_payload=encrypted,
            )
        )
        return channel.id


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    asset_types: list[AssetType] | None = None,
    skip_credential: bool = False,
    skip_touchpoint_for: int = 0,
) -> dict[str, uuid.UUID | list[uuid.UUID]]:
    asset_types = asset_types or [AssetType.social_post]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"sd-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@sd.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()

        start_date = date.today() - timedelta(days=1)
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="sd-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("100.00"),
            currency="USD",
            start_date=start_date,
            end_date=start_date + timedelta(days=14),
            status=CampaignStatus.approval_pending,
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
            payload={
                "channels": [
                    {"platform": "linkedin", "name": "LinkedIn", "allocation_pct": 100,
                     "allocation_amount": "100.00", "rationale": "x", "human_override": False}
                ],
                "kpis": {"primary": {"metric": "mql", "target": 1, "rationale": "z"}, "secondary": []},
            },
            is_accepted=True,
            created_by_kind="agent",
        )
        session.add(proposal)
        await session.flush()

    if not skip_credential:
        channel_id = await _seed_linkedin_credential_and_channel(
            db_engine, tenant.id
        )
    else:
        channel_id = None

    asset_ids: list[uuid.UUID] = []
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for idx, atype in enumerate(asset_types):
            extra_metadata: dict = {"channel_platform": "linkedin", "fields": {"body": f"Body {idx}"}}
            if idx >= skip_touchpoint_for:
                tp = StrategyTouchpoint(
                    tenant_id=tenant.id,
                    proposal_id=proposal.id,
                    channel_platform="linkedin",
                    audience_id=audience.id,
                    scheduled_at=datetime.combine(
                        start_date + timedelta(days=1 + idx),
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
                channel_id=channel_id,
                asset_type=atype,
                status=AssetStatus.approved,
                title=f"Asset {idx}",
                content=f"Body {idx}",
                extra_metadata=extra_metadata,
                is_required=True,
            )
            session.add(asset)
            await session.flush()
            asset_ids.append(asset.id)

    return {
        "tenant_id": tenant.id,
        "campaign_id": campaign.id,
        "channel_id": channel_id,
        "asset_ids": asset_ids,
    }


# ---------------------------------------------------------------------------
# schedule_approved_assets routing
# ---------------------------------------------------------------------------


async def test_schedule_routes_social_post_to_social_dispatch_skill(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_types=[AssetType.social_post])
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tasks = (
            await session.execute(
                select(Task).where(Task.campaign_id == world["campaign_id"])
            )
        ).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].skill_name == "distribution.dispatch_social"


async def test_schedule_routes_mixed_asset_types_correctly(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(
        db_engine,
        asset_types=[AssetType.email, AssetType.social_post, AssetType.blog_post],
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tasks = (
            await session.execute(
                select(Task).where(Task.campaign_id == world["campaign_id"])
            )
        ).scalars().all()
        skills = {t.skill_name for t in tasks}
        # Email + social each get a task; blog_post has no dispatch handler
        # in W30, so it gets no task but still flips to scheduled.
        assert skills == {"distribution.dispatch_email", "distribution.dispatch_social"}

        assets = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()
        assert all(a.status == AssetStatus.scheduled for a in assets)
        blog = next(a for a in assets if a.asset_type == AssetType.blog_post)
        warning = (blog.extra_metadata or {}).get("schedule_warning")
        assert warning and warning["kind"] == "no_dispatch_handler"


# ---------------------------------------------------------------------------
# dispatch_social_asset happy path
# ---------------------------------------------------------------------------


@respx.mock
async def test_dispatch_social_publishes_and_writes_attempt(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_types=[AssetType.social_post])
    respx.post(_UGC_URL).mock(
        return_value=httpx.Response(201, json={"id": "urn:li:share:7777"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        result = await dispatch_social_asset(session, asset_id=world["asset_ids"][0])

    assert result["status"] == "published"
    assert result["provider_post_id"] == "urn:li:share:7777"
    assert result["url"].endswith("urn:li:share:7777/")

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        assert asset.status == AssetStatus.published
        assert asset.published_at is not None
        social_meta = (asset.extra_metadata or {})["social_post"]
        assert social_meta["provider_post_id"] == "urn:li:share:7777"

        attempts = (
            await session.execute(
                select(DispatchAttempt).where(
                    DispatchAttempt.content_asset_id == world["asset_ids"][0]
                )
            )
        ).scalars().all()
        assert len(attempts) == 1
        assert attempts[0].status == "sent"
        assert attempts[0].provider_message_id == "urn:li:share:7777"
        assert attempts[0].recipient_identifier == "urn:li:organization:111"


@respx.mock
async def test_dispatch_social_idempotency_no_double_post(
    db_engine: AsyncEngine,
) -> None:
    """Re-running the same dispatch task hits the dispatch_attempt cache —
    the platform is never called twice."""
    world = await _seed_world(db_engine, asset_types=[AssetType.social_post])
    route = respx.post(_UGC_URL).mock(
        return_value=httpx.Response(201, json={"id": "urn:li:share:once"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)
        await dispatch_social_asset(session, asset_id=world["asset_ids"][0])

    # Roll back to scheduled to simulate a queue retry firing the same task.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        asset.status = AssetStatus.scheduled

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        result = await dispatch_social_asset(session, asset_id=world["asset_ids"][0])

    assert route.call_count == 1  # provider only saw one call
    assert result["provider_post_id"] == "urn:li:share:once"


# ---------------------------------------------------------------------------
# OAuth revoked → campaign paused (E12-S03 #3)
# ---------------------------------------------------------------------------


@respx.mock
async def test_oauth_revoked_pauses_campaign_and_writes_audit(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_types=[AssetType.social_post])
    respx.post(_UGC_URL).mock(
        return_value=httpx.Response(401, json={"message": "revoked"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        out = await dispatch_social_asset(session, asset_id=world["asset_ids"][0])
        # Agent catches OAuthRevokedError, pauses the campaign, returns a
        # failed result so the changes persist (a raise would roll back the
        # session.begin() block, undoing the pause).
        assert out["error"] == "oauth_revoked"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        assert campaign.status == CampaignStatus.paused
        asset = await session.get(ContentAsset, world["asset_ids"][0])
        assert asset.status == AssetStatus.failed

        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "campaign",
                    AuditLog.entity_id == campaign.id,
                    AuditLog.action == "paused",
                )
            )
        ).scalars().all()
        assert any(
            a.extra_metadata.get("reason") == "oauth_revoked" for a in audits
        )


# ---------------------------------------------------------------------------
# Precondition errors
# ---------------------------------------------------------------------------


async def test_dispatch_raises_when_no_credential(db_engine: AsyncEngine) -> None:
    world = await _seed_world(
        db_engine, asset_types=[AssetType.social_post], skip_credential=True
    )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        # Schedule still runs (touchpoint exists); the dispatch is what fails.
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    from app.agents.distribution import DistributionPreconditionError

    with pytest.raises(DistributionPreconditionError):
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            await set_tenant_context(session, world["tenant_id"])
            await dispatch_social_asset(session, asset_id=world["asset_ids"][0])


async def test_dispatch_raises_when_wrong_asset_status(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, asset_types=[AssetType.social_post])
    # Asset is still `approved`, not `scheduled`.
    from app.agents.distribution import DistributionPreconditionError

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, world["tenant_id"])
        with pytest.raises(DistributionPreconditionError):
            await dispatch_social_asset(session, asset_id=world["asset_ids"][0])
