"""W42.3 — Per-asset review workflow + persistent campaign context.

Verifies:
  * `list_drafts_for_review` returns drafted assets with previews.
  * `approve_asset` flips a single asset to approved (manager+).
  * `reject_asset` records the rejection reason on metadata.
  * `where_did_we_leave_off` returns the active campaign + next step.
  * Active-campaign pointer persists across save_history calls and
    survives the message-window trim.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.assistant.memory import (
    MAX_MESSAGES,
    get_active_campaign,
    save_history,
    set_active_campaign,
)
from app.assistant.tools import (
    ToolError,
    ToolPermissionError,
    approve_asset,
    list_drafts_for_review,
    reject_asset,
    where_did_we_leave_off,
)
from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    UserRole,
)
from app.db.models import (
    AppUser,
    Campaign,
    ContentAsset,
    Tenant,
)


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


async def _seed(
    db_engine: AsyncEngine,
    *,
    role: UserRole = UserRole.manager,
    asset_status: AssetStatus = AssetStatus.drafted,
    asset_count: int = 2,
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"appr-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            email=f"u-{uuid.uuid4().hex[:6]}@appr.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        c = Campaign(
            tenant_id=tenant.id,
            owner_id=user.id,
            name="Approval Test",
            campaign_type=CampaignType.demand_gen,
            objective="o",
            brief="b",
            budget_total=Decimal("1000"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
            status=CampaignStatus.content_in_production,
        )
        session.add(c)
        await session.flush()
        asset_ids: list[uuid.UUID] = []
        for i in range(asset_count):
            a = ContentAsset(
                tenant_id=tenant.id,
                campaign_id=c.id,
                asset_type=AssetType.email,
                status=asset_status,
                title=f"Draft {i + 1}",
                content=f"Hi {{first_name}}, this is draft {i + 1}.",
                extra_metadata={"fields": {"subject": f"Draft subject {i + 1}"}},
            )
            session.add(a)
            await session.flush()
            asset_ids.append(a.id)
        return {
            "tenant_id": tenant.id,
            "user": user,
            "campaign_id": c.id,
            "asset_ids": asset_ids,
        }


# ---------------------------------------------------------------------------
# list_drafts_for_review
# ---------------------------------------------------------------------------


async def test_list_drafts_returns_drafted_assets(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, role=UserRole.viewer, asset_count=3)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await list_drafts_for_review(
            session, user=world["user"], campaign="Approval Test"
        )
    assert len(result.data["drafts"]) == 3
    assert result.data["drafts"][0]["status"] == "drafted"
    assert result.data["drafts"][0]["preview"].startswith("Hi {first_name}")


async def test_list_drafts_falls_back_to_active_campaign(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine, asset_count=2)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_active_campaign(
            session,
            user_id=world["user"].id,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await list_drafts_for_review(session, user=world["user"])
    assert len(result.data["drafts"]) == 2
    assert result.data["campaign_name"] == "Approval Test"


async def test_list_drafts_errors_without_active_campaign(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine, asset_count=2)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolError):
            await list_drafts_for_review(session, user=world["user"])


# ---------------------------------------------------------------------------
# approve_asset / reject_asset
# ---------------------------------------------------------------------------


async def test_approve_asset_flips_status(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, asset_count=2)
    aid = world["asset_ids"][0]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await approve_asset(
            session, user=world["user"], asset_id=str(aid)
        )
    assert result.data["status"] == "approved"
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        a = await session.get(ContentAsset, aid)
        assert a.status == AssetStatus.approved


async def test_approve_last_asset_advances_campaign_to_ready_to_launch(
    db_engine: AsyncEngine,
) -> None:
    """Per-asset approval needs to auto-advance the campaign once the
    last required asset is approved — otherwise the campaign sits in
    `approval_pending` forever and the launch tool refuses to fire."""
    from app.audit.context import actor_context

    world = await _seed(db_engine, asset_count=2)
    with actor_context("user", world["user"].id):
        for aid in world["asset_ids"]:
            async with AsyncSession(
                db_engine, expire_on_commit=False
            ) as session, session.begin():
                await approve_asset(
                    session, user=world["user"], asset_id=str(aid)
                )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        c = await session.get(Campaign, world["campaign_id"])
        assert c.status == CampaignStatus.ready_to_launch


async def test_approve_asset_blocked_by_compliance_flag(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine, asset_count=1)
    aid = world["asset_ids"][0]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        a = await session.get(ContentAsset, aid)
        meta = dict(a.extra_metadata or {})
        meta["compliance_violations"] = [
            {"rule": "no-guarantees", "severity": "blocker"}
        ]
        a.extra_metadata = meta

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolError):
            await approve_asset(
                session, user=world["user"], asset_id=str(aid)
            )


async def test_approve_asset_marketer_blocked(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, role=UserRole.marketer, asset_count=1)
    aid = world["asset_ids"][0]
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolPermissionError):
            await approve_asset(
                session, user=world["user"], asset_id=str(aid)
            )


async def test_reject_asset_records_reason(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, asset_count=1)
    aid = world["asset_ids"][0]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await reject_asset(
            session,
            user=world["user"],
            asset_id=str(aid),
            reason="Tone too aggressive — soften the CTA",
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        a = await session.get(ContentAsset, aid)
        assert a.status == AssetStatus.rejected
        assert "Tone too aggressive" in (a.extra_metadata or {}).get(
            "last_rejection_reason", ""
        )


# ---------------------------------------------------------------------------
# where_did_we_leave_off
# ---------------------------------------------------------------------------


async def test_where_did_we_leave_off_returns_active_campaign(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine, asset_count=1)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_active_campaign(
            session,
            user_id=world["user"].id,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await where_did_we_leave_off(session, user=world["user"])
    assert "Approval Test" in result.summary
    assert result.data["status"] == "content_in_production"
    assert result.data["next_step"] == "Review the drafts"


async def test_where_did_we_leave_off_no_active_returns_choices(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine, asset_count=0)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await where_did_we_leave_off(session, user=world["user"])
    assert "choices" in result.data
    assert len(result.data["choices"]) >= 3


# ---------------------------------------------------------------------------
# Active-campaign survives the trim
# ---------------------------------------------------------------------------


async def test_active_campaign_survives_message_trim(db_engine: AsyncEngine) -> None:
    """The whole point of the persistent pointer: even when the message
    window trims to MAX_MESSAGES and drops the early turns that
    mentioned the campaign, the assistant still knows what was active.
    """
    world = await _seed(db_engine, asset_count=1)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_active_campaign(
            session,
            user_id=world["user"].id,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
        )
        # Fill the message window past MAX_MESSAGES with junk turns; the
        # save_history call trims to MAX. The active pointer is in a
        # separate column so it shouldn't be touched.
        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"}
            for i in range(MAX_MESSAGES * 3)
        ]
        await save_history(
            session,
            user_id=world["user"].id,
            tenant_id=world["tenant_id"],
            messages=msgs,
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        active = await get_active_campaign(session, user_id=world["user"].id)
    assert active == world["campaign_id"]
