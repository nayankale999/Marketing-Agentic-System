"""W42.2 — Orchestration tools (synthesise_audience, accept_strategy,
approve_all_content, launch_campaign, request_input).

We don't stub Anthropic for `generate_strategy` / `generate_content`
here — those need a live model. Those flows are covered by their own
existing tests (strategist + content_creator); this file verifies the
assistant-tool wrappers + the non-LLM tools."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.assistant.tools import (
    ToolError,
    ToolPermissionError,
    accept_strategy,
    approve_all_content,
    launch_campaign,
    request_input,
    synthesise_audience,
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
    Campaign,
    Channel,
    ContentAsset,
    StrategyProposal,
    StrategyTouchpoint,
    Tenant,
)


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------


async def _seed(
    db_engine: AsyncEngine, *, role: UserRole = UserRole.marketer
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"orch-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            email=f"u-{uuid.uuid4().hex[:6]}@orch.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        ch_email = Channel(
            tenant_id=tenant.id,
            name="Email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        session.add(ch_email)
        await session.flush()
        c = Campaign(
            tenant_id=tenant.id,
            owner_id=user.id,
            name="Test Push",
            campaign_type=CampaignType.demand_gen,
            objective="100 MQLs",
            brief="b",
            budget_total=Decimal("1000"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
            status=CampaignStatus.drafted,
        )
        session.add(c)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "user": user,
            "campaign_id": c.id,
            "email_channel_id": ch_email.id,
        }


# ---------------------------------------------------------------------------
# request_input
# ---------------------------------------------------------------------------


async def test_request_input_short_circuits_and_returns_choices(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await request_input(
            session,
            user=world["user"],
            prompt="Pick a campaign type",
            options=["demand_gen", "lead_gen", "product_launch"],
        )
    assert result.summary == "Pick a campaign type"
    assert result.requires_confirmation is True
    assert len(result.data["choices"]) == 3
    # Plain strings normalise to {value, label}.
    assert result.data["choices"][0] == {"value": "demand_gen", "label": "demand_gen"}


async def test_request_input_normalises_label_value_dicts(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await request_input(
            session,
            user=world["user"],
            prompt="Pick one",
            options=[
                {"value": "yes", "label": "Yes, proceed"},
                {"value": "no", "label": "Not now"},
            ],
        )
    assert result.data["choices"][0]["label"] == "Yes, proceed"
    assert result.data["choices"][1]["value"] == "no"


# ---------------------------------------------------------------------------
# synthesise_audience
# ---------------------------------------------------------------------------


async def test_synthesise_audience_creates_members_and_advances_status(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await synthesise_audience(
            session,
            user=world["user"],
            campaign="Test Push",
            size=15,
            persona="Demo CTOs",
        )
    assert result.data["size"] == 15
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        c = await session.get(Campaign, world["campaign_id"])
        assert c.status == CampaignStatus.audience_built
        members = (
            await session.execute(
                select(AudienceMember).join(
                    Audience, Audience.id == AudienceMember.audience_id
                ).where(Audience.campaign_id == world["campaign_id"])
            )
        ).scalars().all()
        assert len(members) == 15


async def test_synthesise_audience_size_validation(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolError):
            await synthesise_audience(
                session, user=world["user"], campaign="Test Push", size=500
            )


async def test_synthesise_audience_viewer_blocked(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, role=UserRole.viewer)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolPermissionError):
            await synthesise_audience(
                session, user=world["user"], campaign="Test Push"
            )


# ---------------------------------------------------------------------------
# accept_strategy
# ---------------------------------------------------------------------------


async def _seed_with_proposal(db_engine: AsyncEngine) -> dict:
    world = await _seed(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # The synth audience is a precondition for seed_calendar.
        await synthesise_audience(
            session, user=world["user"], campaign="Test Push", size=10
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        prop = StrategyProposal(
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            version=1,
            is_accepted=False,
            created_by_kind="agent",
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
                    "primary": {"metric": "conversion", "target": 50, "rationale": "x"},
                    "secondary": [],
                },
            },
        )
        session.add(prop)
        await session.flush()
        world["proposal_id"] = prop.id
    return world


async def test_accept_strategy_requires_confirmation_first(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_with_proposal(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await accept_strategy(
            session, user=world["user"], campaign="Test Push"
        )
    assert result.requires_confirmation is True


async def test_accept_strategy_seeds_calendar(db_engine: AsyncEngine) -> None:
    world = await _seed_with_proposal(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await accept_strategy(
            session, user=world["user"], campaign="Test Push", confirm=True
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        prop = await session.get(StrategyProposal, world["proposal_id"])
        assert prop.is_accepted is True
        tps = (
            await session.execute(
                select(StrategyTouchpoint).where(
                    StrategyTouchpoint.proposal_id == prop.id
                )
            )
        ).scalars().all()
        assert len(tps) >= 1
        c = await session.get(Campaign, world["campaign_id"])
        assert c.status == CampaignStatus.strategy_set


async def test_accept_strategy_errors_with_no_proposal(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolError):
            await accept_strategy(
                session,
                user=world["user"],
                campaign="Test Push",
                confirm=True,
            )


# ---------------------------------------------------------------------------
# approve_all_content
# ---------------------------------------------------------------------------


async def test_approve_all_content_bulk_approves(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, role=UserRole.manager)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for status in (AssetStatus.drafted, AssetStatus.pending_approval):
            session.add(
                ContentAsset(
                    tenant_id=world["tenant_id"],
                    campaign_id=world["campaign_id"],
                    channel_id=world["email_channel_id"],
                    asset_type=AssetType.email,
                    status=status,
                    content="x",
                )
            )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await approve_all_content(
            session, user=world["user"], campaign="Test Push", confirm=True
        )
    assert result.data["approved"] == 2

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()
        assert all(r.status == AssetStatus.approved for r in rows)


async def test_approve_all_content_marketer_blocked(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, role=UserRole.marketer)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolPermissionError):
            await approve_all_content(
                session,
                user=world["user"],
                campaign="Test Push",
                confirm=True,
            )


# ---------------------------------------------------------------------------
# launch_campaign
# ---------------------------------------------------------------------------


async def test_launch_campaign_flips_to_live(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, role=UserRole.manager)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        c = await session.get(Campaign, world["campaign_id"])
        c.status = CampaignStatus.ready_to_launch

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await launch_campaign(
            session, user=world["user"], campaign="Test Push", confirm=True
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        c = await session.get(Campaign, world["campaign_id"])
        assert c.status == CampaignStatus.live
        assert c.launched_at is not None


async def test_launch_campaign_rejects_non_ready_state(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(db_engine, role=UserRole.manager)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolError):
            await launch_campaign(
                session,
                user=world["user"],
                campaign="Test Push",
                confirm=True,
            )
