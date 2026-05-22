"""W23 — A/B variant generation (E06-S05).

Two layers under test:

  * Seed-time fan-out — strategy_proposal.payload.ab_tests creates N variant
    content_asset rows + an ab_test row linking variant_a/variant_b.
  * Variant generation — similarity check after non-baseline variant lands
    triggers regeneration with a differentiation directive.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from decimal import Decimal

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents._variants import (
    MAX_VARIANTS,
    angle_for_index,
    parse_ab_test_specs,
)
from app.agents.content_creator import (
    generate_asset,
    seed_assets_for_campaign,
)
from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AbTest,
    AppUser,
    Audience,
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
# Pure helper tests
# ---------------------------------------------------------------------------


def test_parse_ab_test_specs_drops_malformed() -> None:
    payload = {
        "ab_tests": [
            {"channel": "email", "variants": 2},
            {"channel": "", "variants": 2},  # dropped
            {"variants": 3},  # dropped (no channel)
            "not a dict",  # dropped
            {"channel": "linkedin", "variants": 99},  # clamped to MAX_VARIANTS
            {"channel": "x", "variants": 1},  # clamped to MIN_VARIANTS
        ]
    }
    specs = parse_ab_test_specs(payload)
    by_channel = {s.channel: s for s in specs}
    assert "email" in by_channel
    assert by_channel["email"].variants == 2
    assert by_channel["linkedin"].variants == MAX_VARIANTS
    assert by_channel["x"].variants == 2  # min clamp


def test_parse_ab_test_specs_no_ab_tests_key() -> None:
    assert parse_ab_test_specs({}) == []
    assert parse_ab_test_specs({"ab_tests": "not a list"}) == []


def test_angle_for_index_baseline_is_empty() -> None:
    assert angle_for_index(0) == ""


def test_angle_for_index_returns_distinct_angles() -> None:
    angles = {angle_for_index(i) for i in range(1, MAX_VARIANTS)}
    assert len(angles) == MAX_VARIANTS - 1  # each index has a distinct angle


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


def _anthropic_response(payload: dict) -> httpx.Response:
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


def _email_payload(body: str) -> dict:
    return {
        "subject": "S",
        "preheader": "P",
        "body": body,
        "cta": "Try it",
    }


def _copywriting_tool() -> CopywritingTool:
    return CopywritingTool(
        client=AsyncAnthropic(api_key="test-key"), model="claude-sonnet-4-6"
    )


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    variants: int = 2,
) -> dict[str, uuid.UUID]:
    """Tenant + email channel + campaign + accepted proposal with one email
    touchpoint flagged for A/B testing with `variants` variants."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ab-{uuid.uuid4().hex[:6]}")
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
            name="ab-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=CampaignStatus.strategy_set,
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
                        "allocation_amount": "10000.00",
                        "rationale": "x",
                        "human_override": False,
                    }
                ],
                "kpis": {
                    "primary": {"metric": "mql", "target": 100, "rationale": "z"},
                    "secondary": [],
                },
                "ab_tests": [{"channel": "email", "variants": variants}],
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
                channel_platform="email",
                audience_id=audience.id,
                scheduled_at=datetime.combine(date(2026, 6, 15), time(9, 0), UTC),
            )
        )
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
        }


# ---------------------------------------------------------------------------
# Seed fan-out
# ---------------------------------------------------------------------------


async def test_seed_creates_two_variants_and_ab_test_row(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine, variants=2)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        rows = await seed_assets_for_campaign(session, campaign=campaign)

    assert len(rows) == 2

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        ab_tests = (
            await session.execute(
                select(AbTest).where(AbTest.campaign_id == world["campaign_id"])
            )
        ).scalars().all()
        assert len(ab_tests) == 1
        ab = ab_tests[0]
        assert ab.status == AbTestStatus.designing
        assert ab.variant_a_id is not None
        assert ab.variant_b_id is not None
        assert ab.variant_a_id != ab.variant_b_id

        assets = (
            await session.execute(
                select(ContentAsset).where(ContentAsset.campaign_id == world["campaign_id"])
            )
        ).scalars().all()
        groups = {a.extra_metadata.get("ab_test_group_id") for a in assets}
        assert groups == {str(ab.id)}
        indexes = sorted(a.extra_metadata.get("variant_index") for a in assets)
        assert indexes == [0, 1]


async def test_seed_creates_up_to_five_variants(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine, variants=5)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        rows = await seed_assets_for_campaign(session, campaign=campaign)
    assert len(rows) == 5

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        ab = (
            await session.execute(
                select(AbTest).where(AbTest.campaign_id == world["campaign_id"])
            )
        ).scalar_one()
        # Only the first two go into the canonical columns; the rest are
        # joined via metadata.
        assert ab.variant_a_id is not None
        assert ab.variant_b_id is not None


async def test_seed_without_ab_spec_creates_single_asset(db_engine: AsyncEngine) -> None:
    """Sanity check the existing non-A/B path still works after the changes."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"plain-{uuid.uuid4().hex[:6]}")
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
            name="plain",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 14),
            status=CampaignStatus.strategy_set,
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
                        "allocation_amount": "10000.00",
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
                channel_platform="email",
                audience_id=audience.id,
                scheduled_at=datetime.combine(date(2026, 6, 7), time(9, 0), UTC),
            )
        )
        await session.flush()
        tenant_id = tenant.id
        campaign_id = campaign.id

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        rows = await seed_assets_for_campaign(session, campaign=campaign)
    assert len(rows) == 1
    assert rows[0].extra_metadata.get("variant_index") is None


# ---------------------------------------------------------------------------
# Variant generation + similarity retry
# ---------------------------------------------------------------------------


@respx.mock
async def test_baseline_then_variant_no_regen_when_dissimilar(
    db_engine: AsyncEngine,
) -> None:
    """Baseline generates, then variant B generates with a clearly different
    body — similarity is low, no retry needed."""
    respx.post(_API).mock(
        side_effect=[
            _anthropic_response(
                _email_payload(
                    "Cut deploys to five minutes with our pipeline. "
                    "Three customers reported speedups already."
                )
            ),
            _anthropic_response(
                _email_payload(
                    "Avoid late-night rollbacks. Our checks catch failures "
                    "before customers see them in production."
                )
            ),
        ]
    )

    world = await _seed_world(db_engine, variants=2)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        assets = (
            await session.execute(
                select(ContentAsset)
                .where(ContentAsset.campaign_id == world["campaign_id"])
                .order_by(ContentAsset.extra_metadata["variant_index"].as_integer())
            )
        ).scalars().all()

    # Generate baseline first, then variant B
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
        variant_b = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"],
                    ContentAsset.extra_metadata["variant_index"].as_integer() == 1,
                )
            )
        ).scalar_one()
        sim = variant_b.extra_metadata["variant_similarity"]
        assert sim["passed"] is True
        assert sim["score"] < 0.9


@respx.mock
async def test_too_similar_variant_triggers_regeneration(
    db_engine: AsyncEngine,
) -> None:
    """Variant B comes out near-identical to baseline → similarity > 0.9 →
    agent retries with a differentiation directive; the second body is
    different enough to pass."""
    baseline_body = (
        "Marketing automation lets teams move faster. "
        "Marketing automation is the future of marketing."
    )
    near_duplicate_body = baseline_body  # cosine == 1.0
    differentiated_body = (
        "Avoid duplicate work with our pipeline. We catch regressions early "
        "so launches don't slip."
    )

    respx.post(_API).mock(
        side_effect=[
            _anthropic_response(_email_payload(baseline_body)),
            _anthropic_response(_email_payload(near_duplicate_body)),
            _anthropic_response(_email_payload(differentiated_body)),
        ]
    )

    world = await _seed_world(db_engine, variants=2)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        assets = (
            await session.execute(
                select(ContentAsset)
                .where(ContentAsset.campaign_id == world["campaign_id"])
                .order_by(ContentAsset.extra_metadata["variant_index"].as_integer())
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
        variant_b = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"],
                    ContentAsset.extra_metadata["variant_index"].as_integer() == 1,
                )
            )
        ).scalar_one()
        sim = variant_b.extra_metadata["variant_similarity"]
        # Final body landed below the threshold after the retry.
        assert sim["passed"] is True


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_world(override_api_db, db_engine: AsyncEngine):
    return await _seed_world(db_engine, variants=2)


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@ab.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


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


async def test_list_and_detail_ab_tests(client_as, api_world, db_engine) -> None:
    # Seed assets to create the ab_test row.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, api_world["tenant_id"])
        campaign = await session.get(Campaign, api_world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)

    client, _ = await client_as(UserRole.viewer)
    list_resp = await client.get(
        f"/api/campaigns/{api_world['campaign_id']}/ab-tests"
    )
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    ab_id = list_resp.json()["items"][0]["id"]
    detail = await client.get(f"/api/ab-tests/{ab_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert len(body["variant_ids"]) == 2


async def test_add_variant_extends_group_up_to_five(
    client_as, api_world, db_engine, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, api_world["tenant_id"])
        campaign = await session.get(Campaign, api_world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)

    client, _ = await client_as(UserRole.marketer)
    ab_id = (
        await client.get(f"/api/campaigns/{api_world['campaign_id']}/ab-tests")
    ).json()["items"][0]["id"]

    # Seed created 2; we should be able to add 3 more, then hit the cap.
    for expected_index in (2, 3, 4):
        resp = await client.post(f"/api/ab-tests/{ab_id}/add-variant")
        assert resp.status_code == 202, resp.text
        assert resp.json()["variant_index"] == expected_index

    cap_resp = await client.post(f"/api/ab-tests/{ab_id}/add-variant")
    assert cap_resp.status_code == 409


async def test_add_variant_returns_503_without_api_key(
    client_as, api_world, db_engine, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, api_world["tenant_id"])
        campaign = await session.get(Campaign, api_world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)

    client, _ = await client_as(UserRole.marketer)
    ab_id = (
        await client.get(f"/api/campaigns/{api_world['campaign_id']}/ab-tests")
    ).json()["items"][0]["id"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    resp = await client.post(f"/api/ab-tests/{ab_id}/add-variant")
    assert resp.status_code == 503


async def test_viewer_cannot_add_variant(client_as, api_world, db_engine, monkeypatch) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, api_world["tenant_id"])
        campaign = await session.get(Campaign, api_world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)

    client, _ = await client_as(UserRole.viewer)
    ab_id = (
        await client.get(f"/api/campaigns/{api_world['campaign_id']}/ab-tests")
    ).json()["items"][0]["id"]
    resp = await client.post(f"/api/ab-tests/{ab_id}/add-variant")
    assert resp.status_code == 403
