"""W22 — Content Creator agent (E06-S01, E06-S04).

Three layers under test:

  * `_content_planner` — pure helpers (touch→asset_type mapping, prompt
    composition, metadata bundling).
  * Agent + state-machine wiring — DB-backed, respx-mocked Anthropic.
  * API surface — start/list/get/regenerate endpoints, role gating, state
    guards.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents._content_planner import (
    AssetPlan,
    PlannerError,
    build_copywriting_inputs,
    bundle_metadata,
    extract_title,
    plan_for_platform,
)
from app.agents.content_creator import (
    ContentCreatorError,
    ensure_content_creator_agent,
    generate_asset,
    seed_assets_for_campaign,
)
from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AgentKind,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    Agent,
    AppUser,
    Audience,
    BrandVoice,
    Campaign,
    Channel,
    ContentAsset,
    StrategyProposal,
    StrategyTouchpoint,
    Task,
    Tenant,
)
from app.db.session import set_tenant_context
from app.tools.copywriting import CopywritingTool
from app.tools.seo import SeoAnalysisTool


_API = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# Planner unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform,expected_asset_type",
    [
        ("email", AssetType.email),
        ("linkedin", AssetType.social_post),
        ("x", AssetType.social_post),
        ("meta", AssetType.social_post),
        ("instagram", AssetType.social_post),
        ("google_ads", AssetType.ad_creative),
        ("meta_ads", AssetType.ad_creative),
        ("blog", AssetType.blog_post),
        ("web", AssetType.landing_page_copy),
        ("sms", AssetType.sms),
    ],
)
def test_plan_for_platform_maps_every_channel(
    platform: str, expected_asset_type: AssetType
) -> None:
    plan = plan_for_platform(platform)
    assert plan.asset_type is expected_asset_type
    assert plan.tool_channel  # non-empty


def test_plan_for_platform_raises_on_unknown() -> None:
    with pytest.raises(PlannerError):
        plan_for_platform("not_a_channel")


def test_plan_for_platform_marks_long_form_for_seo() -> None:
    assert plan_for_platform("blog").requires_seo is True
    assert plan_for_platform("web").requires_seo is True
    assert plan_for_platform("email").requires_seo is False
    assert plan_for_platform("linkedin").requires_seo is False


def test_plan_for_platform_picks_per_platform_tool_channel() -> None:
    # linkedin and x both map to social_post but the tool channel differs so
    # the CopywritingTool can apply the right length cap.
    assert plan_for_platform("linkedin").tool_channel == "linkedin"
    assert plan_for_platform("x").tool_channel == "x"
    assert plan_for_platform("meta").tool_channel == "social_post"


def test_build_copywriting_inputs_includes_position_and_voice() -> None:
    plan = plan_for_platform("email")
    inputs = build_copywriting_inputs(
        plan=plan,
        campaign_brief="Launch a new SMB tier",
        campaign_objective="Acquire 500 MQLs",
        audience_summary="EMEA SMBs",
        voice_prompt="Tone: confident, concise",
        touchpoint_position=2,
        total_touchpoints_for_channel=4,
        seed="abc",
    )
    assert inputs["channel"] == "email"
    assert "Touch 2 of 4" in inputs["brief"]
    assert inputs["voice"] == "Tone: confident, concise"
    assert inputs["audience_summary"] == "EMEA SMBs"
    assert inputs["seed"] == "abc"


def test_build_copywriting_inputs_includes_keywords_only_for_seo_types() -> None:
    blog_inputs = build_copywriting_inputs(
        plan=plan_for_platform("blog"),
        campaign_brief="brief",
        campaign_objective="x",
        audience_summary="",
        voice_prompt=None,
        touchpoint_position=1,
        total_touchpoints_for_channel=1,
        target_keywords=["marketing automation", "lifecycle"],
    )
    assert "marketing automation" in blog_inputs["brief"]

    email_inputs = build_copywriting_inputs(
        plan=plan_for_platform("email"),
        campaign_brief="brief",
        campaign_objective="x",
        audience_summary="",
        voice_prompt=None,
        touchpoint_position=1,
        total_touchpoints_for_channel=1,
        target_keywords=["marketing automation"],
    )
    assert "marketing automation" not in email_inputs["brief"]


def test_extract_title_priority() -> None:
    plan = plan_for_platform("email")
    assert extract_title(plan, {"subject": "Hi", "body": "..."}) == "Hi"
    assert (
        extract_title(plan, {"headline": "Big idea", "body": "..."}) == "Big idea"
    )
    assert (
        extract_title(plan_for_platform("x"), {"body": "Just a tweet"})
        == "Just a tweet"
    )
    assert extract_title(plan, {"body": ""}) is None


def test_bundle_metadata_includes_optional_sections() -> None:
    plan = plan_for_platform("email")
    cw = {
        "subject": "S",
        "preheader": "P",
        "body": "B",
        "cta": "C",
        "length_metrics": {"body": 1},
        "length_warning": {"body": 5},
    }
    brand = {"pass": False, "failing_words": ["synergy"]}
    seo = {"score": 80, "keyword_density": {}}
    out = bundle_metadata(
        copywriting_output=cw, brand_check=brand, seo=seo, storage_uri="s3://x"
    )
    assert out["storage_uri"] == "s3://x"
    assert out["fields"] == {"subject": "S", "preheader": "P", "cta": "C"}
    assert out["brand_check"] == brand
    assert out["seo"] == seo
    assert out["length_warning"] == {"body": 5}


def test_bundle_metadata_omits_seo_when_absent() -> None:
    out = bundle_metadata(
        copywriting_output={"body": "B", "length_metrics": {}},
        brand_check={"pass": True, "failing_words": []},
        seo=None,
    )
    assert "seo" not in out


# ---------------------------------------------------------------------------
# Helpers — Anthropic response + DB seeding
# ---------------------------------------------------------------------------


def _anthropic_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )


def _copywriting_payload_email() -> dict:
    return {
        "subject": "Cut deploys to 5 min",
        "preheader": "How Acme shipped faster",
        "body": "Hi {first_name}, our pipeline rewrite trimmed deploys.",
        "cta": "Book a demo",
    }


def _copywriting_tool() -> CopywritingTool:
    return CopywritingTool(
        client=AsyncAnthropic(api_key="test-key"), model="claude-sonnet-4-6"
    )


def _payload(*, email_pct: int = 60, linkedin_pct: int = 40) -> dict:
    budget = Decimal("10000.00")
    return {
        "channels": [
            {
                "platform": "email",
                "name": "Email",
                "allocation_pct": email_pct,
                "allocation_amount": str(
                    (budget * Decimal(email_pct) / 100).quantize(Decimal("0.01"))
                ),
                "rationale": "x",
                "human_override": False,
            },
            {
                "platform": "linkedin",
                "name": "LinkedIn",
                "allocation_pct": linkedin_pct,
                "allocation_amount": str(
                    (budget * Decimal(linkedin_pct) / 100).quantize(Decimal("0.01"))
                ),
                "rationale": "y",
                "human_override": False,
            },
        ],
        "kpis": {"primary": {"metric": "mql", "target": 500, "rationale": "z"}, "secondary": []},
        "summary_rationale": "x",
    }


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    accepted: bool = True,
    with_touchpoints: bool = True,
    state: CampaignStatus = CampaignStatus.strategy_set,
) -> dict[str, uuid.UUID]:
    """Seed tenant + channels + audience + campaign + accepted proposal +
    touchpoints. Returns a dict of ids the test picks from."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"cc-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()

        for platform, name in [
            (ChannelPlatform.email, "Email"),
            (ChannelPlatform.linkedin, "LinkedIn"),
        ]:
            session.add(
                Channel(tenant_id=tenant.id, name=name, platform=platform, is_active=True)
            )

        campaign = Campaign(
            tenant_id=tenant.id,
            name="cc-camp",
            campaign_type=CampaignType.product_launch,
            objective="Acquire 500 MQLs in EMEA",
            brief="Launch a new SMB tier",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=state,
        )
        session.add(campaign)
        await session.flush()

        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seg",
            segment_criteria={},
            estimated_size=100,
            actual_size=100,
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()

        proposal = StrategyProposal(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            version=1,
            payload=_payload(),
            is_accepted=accepted,
            created_by_kind="agent",
        )
        session.add(proposal)
        await session.flush()

        if with_touchpoints:
            for i, platform in enumerate(("email", "linkedin")):
                session.add(
                    StrategyTouchpoint(
                        tenant_id=tenant.id,
                        proposal_id=proposal.id,
                        channel_platform=platform,
                        audience_id=audience.id,
                        scheduled_at=datetime.combine(
                            date(2026, 6, 7 + i * 7), time(9, 0), UTC
                        ),
                        position=i,
                    )
                )
            await session.flush()

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "proposal_id": proposal.id,
            "audience_id": audience.id,
        }


# ---------------------------------------------------------------------------
# Agent integration tests
# ---------------------------------------------------------------------------


async def test_ensure_content_creator_agent_is_idempotent(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        a = await ensure_content_creator_agent(session, world["tenant_id"])
        b = await ensure_content_creator_agent(session, world["tenant_id"])
        assert a.id == b.id
        assert a.agent_type == AgentKind.content_creator


async def test_seed_assets_creates_one_per_touchpoint_and_enqueues_tasks(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        rows = await seed_assets_for_campaign(session, campaign=campaign)
        assert len(rows) == 2
        assert {r.asset_type for r in rows} == {AssetType.email, AssetType.social_post}
        assert all(r.status == AssetStatus.requested for r in rows)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tasks = (
            await session.execute(
                select(Task).where(Task.campaign_id == world["campaign_id"])
            )
        ).scalars().all()
        assert len(tasks) == 2
        assert all(t.skill_name == "content_creator.generate_asset" for t in tasks)


async def test_seed_assets_raises_without_accepted_proposal(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, accepted=False)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        with pytest.raises(ContentCreatorError):
            await seed_assets_for_campaign(session, campaign=campaign)


async def test_seed_assets_raises_when_calendar_is_empty(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine, with_touchpoints=False)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        with pytest.raises(ContentCreatorError):
            await seed_assets_for_campaign(session, campaign=campaign)


@respx.mock
async def test_generate_asset_happy_path_drafts_with_metadata(
    db_engine: AsyncEngine,
) -> None:
    respx.post(_API).mock(return_value=_anthropic_response(_copywriting_payload_email()))

    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        email_asset = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"],
                    ContentAsset.asset_type == AssetType.email,
                )
            )
        ).scalar_one()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        out = await generate_asset(
            session,
            asset_id=email_asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )

    assert out["status"] == "drafted"
    assert out["brand_check_pass"] is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        reloaded = await session.get(ContentAsset, email_asset.id)
        assert reloaded.status == AssetStatus.drafted
        assert reloaded.title == "Cut deploys to 5 min"
        assert "brand_check" in reloaded.extra_metadata
        assert "fields" in reloaded.extra_metadata
        assert reloaded.extra_metadata["fields"]["subject"] == "Cut deploys to 5 min"


@respx.mock
async def test_generate_asset_reverts_to_requested_on_failure(
    db_engine: AsyncEngine,
) -> None:
    # Model returns invalid JSON → CopywritingError → revert to requested.
    respx.post(_API).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "not json"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )

    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        email_asset = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"],
                    ContentAsset.asset_type == AssetType.email,
                )
            )
        ).scalar_one()

    with pytest.raises(Exception):
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            await set_tenant_context(session, world["tenant_id"])
            await generate_asset(
                session,
                asset_id=email_asset.id,
                copywriting_tool=_copywriting_tool(),
                seo_tool=SeoAnalysisTool(),
            )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        reloaded = await session.get(ContentAsset, email_asset.id)
        assert reloaded.status == AssetStatus.requested


@respx.mock
async def test_generate_asset_runs_seo_for_long_form(db_engine: AsyncEngine) -> None:
    # Seed a campaign with a blog touchpoint + target_keywords on the campaign.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"seo-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        session.add(
            Channel(tenant_id=tenant.id, name="Blog", platform=ChannelPlatform.blog, is_active=True)
        )
        campaign = Campaign(
            tenant_id=tenant.id,
            name="seo-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("1000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            kpi_targets={"target_keywords": ["marketing automation"]},
            status=CampaignStatus.strategy_set,
        )
        session.add(campaign)
        await session.flush()
        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="a",
            segment_criteria={},
            estimated_size=10,
            actual_size=10,
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
                    {
                        "platform": "blog",
                        "name": "Blog",
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
        session.add(
            StrategyTouchpoint(
                tenant_id=tenant.id,
                proposal_id=proposal.id,
                channel_platform="blog",
                audience_id=audience.id,
                scheduled_at=datetime.combine(date(2026, 6, 15), time(9, 0), UTC),
            )
        )
        tenant_id = tenant.id
        campaign_id = campaign.id

    blog_body = (
        "Marketing automation is the future of marketing. "
        + ("marketing automation lets teams move faster. " * 80)
    )
    respx.post(_API).mock(
        return_value=_anthropic_response(
            {
                "title": "Marketing automation playbook",
                "meta_description": (
                    "A practical 5-step guide to marketing automation for SMB " * 3
                )[:150],
                "body": blog_body,
            }
        )
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        await seed_assets_for_campaign(session, campaign=campaign)
        blog_asset = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == campaign_id,
                    ContentAsset.asset_type == AssetType.blog_post,
                )
            )
        ).scalar_one()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        out = await generate_asset(
            session,
            asset_id=blog_asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )
        assert out["seo_score"] is not None

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        reloaded = await session.get(ContentAsset, blog_asset.id)
        assert "seo" in reloaded.extra_metadata
        assert "keyword_density" in reloaded.extra_metadata["seo"]


@respx.mock
async def test_brand_check_flags_dont_words(db_engine: AsyncEngine) -> None:
    payload = _copywriting_payload_email()
    payload["body"] = "Let's disrupt the market with synergy. Hi {first_name}."

    respx.post(_API).mock(return_value=_anthropic_response(payload))

    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        # Active brand voice with dont_words.
        voice = BrandVoice(
            tenant_id=world["tenant_id"],
            name="strict",
            is_active=True,
            tone_descriptors=["confident"],
            do_words=[],
            dont_words=["disrupt", "synergy"],
            sample_paragraphs=[],
        )
        session.add(voice)
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        email_asset = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"],
                    ContentAsset.asset_type == AssetType.email,
                )
            )
        ).scalar_one()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        out = await generate_asset(
            session,
            asset_id=email_asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )
        assert out["brand_check_pass"] is False

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        reloaded = await session.get(ContentAsset, email_asset.id)
        assert reloaded.status == AssetStatus.drafted  # still drafted, just flagged
        assert set(reloaded.extra_metadata["brand_check"]["failing_words"]) == {
            "disrupt",
            "synergy",
        }


@respx.mock
async def test_all_drafted_advances_to_approval_pending(
    db_engine: AsyncEngine,
) -> None:
    # Universal payload — covers email's (subject/preheader/body/cta) AND
    # linkedin's (headline/body/cta) required fields in one response so the
    # same mock works for both touchpoints in the campaign.
    respx.post(_API).mock(
        return_value=_anthropic_response(
            {
                "subject": "S",
                "preheader": "P",
                "headline": "H",
                "body": "Some body text.",
                "cta": "CTA",
            }
        )
    )

    world = await _seed_world(db_engine, state=CampaignStatus.content_in_production)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        assets = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()

    for asset in assets:
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            await set_tenant_context(session, world["tenant_id"])
            await generate_asset(
                session,
                asset_id=asset.id,
                copywriting_tool=_copywriting_tool(),
                seo_tool=SeoAnalysisTool(),
            )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        assert campaign.status == CampaignStatus.approval_pending


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@cc.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def api_world(override_api_db, db_engine: AsyncEngine):
    """Pre-seeded campaign in strategy_set state, ready for /start."""
    world = await _seed_world(db_engine)
    return world


@pytest.fixture
async def client_as(
    api_world,
    db_engine: AsyncEngine,
) -> AsyncIterator:
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


async def test_start_content_returns_503_without_api_key(
    client_as, api_world, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(
        f"/api/campaigns/{api_world['campaign_id']}/content/start"
    )
    assert resp.status_code == 503


async def test_start_content_seeds_assets_and_transitions_state(
    client_as, api_world, db_engine, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(
        f"/api/campaigns/{api_world['campaign_id']}/content/start"
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "content_in_production"
    assert body["assets_created"] == 2

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, api_world["campaign_id"])
        assert campaign.status == CampaignStatus.content_in_production


async def test_start_content_rejects_wrong_state(
    client_as, db_engine, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    world = await _seed_world(db_engine, state=CampaignStatus.drafted)
    user = await _make_user(db_engine, world["tenant_id"], UserRole.marketer)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/campaigns/{world['campaign_id']}/content/start")
            assert resp.status_code == 409
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_viewer_cannot_start_content(client_as, api_world, monkeypatch) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post(
        f"/api/campaigns/{api_world['campaign_id']}/content/start"
    )
    assert resp.status_code == 403


async def test_list_content_assets_filters_by_status(
    client_as, api_world, db_engine, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    client, _ = await client_as(UserRole.marketer)
    await client.post(f"/api/campaigns/{api_world['campaign_id']}/content/start")

    listed = await client.get(
        f"/api/campaigns/{api_world['campaign_id']}/content-assets"
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 2
    assert all(it["status"] == "requested" for it in items)

    filtered = await client.get(
        f"/api/campaigns/{api_world['campaign_id']}/content-assets?status=drafted"
    )
    assert filtered.status_code == 200
    assert filtered.json()["items"] == []


async def test_get_asset_detail(client_as, api_world, monkeypatch) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    client, _ = await client_as(UserRole.marketer)
    await client.post(f"/api/campaigns/{api_world['campaign_id']}/content/start")
    listed = (
        await client.get(
            f"/api/campaigns/{api_world['campaign_id']}/content-assets"
        )
    ).json()
    asset_id = listed["items"][0]["id"]

    resp = await client.get(f"/api/content-assets/{asset_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == asset_id


async def test_regenerate_flips_status_and_enqueues(
    client_as, api_world, db_engine, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    client, _ = await client_as(UserRole.marketer)
    await client.post(f"/api/campaigns/{api_world['campaign_id']}/content/start")
    listed = (
        await client.get(
            f"/api/campaigns/{api_world['campaign_id']}/content-assets"
        )
    ).json()
    asset_id = listed["items"][0]["id"]

    # Hand-toggle the asset to drafted so regenerate has work to do.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = await session.get(ContentAsset, uuid.UUID(asset_id))
        asset.status = AssetStatus.drafted

    # Count tasks before so we can assert one was added.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        before_count = (
            await session.execute(
                select(Task).where(Task.campaign_id == api_world["campaign_id"])
            )
        ).scalars().all()

    resp = await client.post(f"/api/content-assets/{asset_id}/regenerate")
    assert resp.status_code == 202
    assert resp.json()["status"] == "requested"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        after_count = (
            await session.execute(
                select(Task).where(Task.campaign_id == api_world["campaign_id"])
            )
        ).scalars().all()
        assert len(after_count) == len(before_count) + 1


async def test_regenerate_rejects_when_campaign_in_wrong_state(
    client_as, api_world, db_engine, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    client, _ = await client_as(UserRole.marketer)
    await client.post(f"/api/campaigns/{api_world['campaign_id']}/content/start")
    listed = (
        await client.get(
            f"/api/campaigns/{api_world['campaign_id']}/content-assets"
        )
    ).json()
    asset_id = listed["items"][0]["id"]

    # Roll campaign back to strategy_set — regenerate shouldn't be valid.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        campaign = await session.get(Campaign, api_world["campaign_id"])
        campaign.status = CampaignStatus.strategy_set

    resp = await client.post(f"/api/content-assets/{asset_id}/regenerate")
    assert resp.status_code == 409
