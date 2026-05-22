"""W20 — Campaign Strategist agent (E05-S01, E05-S02, E05-S05).

Three layers under test:

  * Planner — pure logic + LLM call. Anthropic intercepted with respx.
  * Agent module — `assert_preconditions`, `propose` writing to DB.
  * API surface — enqueue / latest / history / patch overrides / accept.

End-to-end queue-driven runs are out of scope here; the worker/handler
plumbing is covered by tests/test_queue.py and tests/test_tools.py.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents._strategist_planner import (
    ChannelInfo,
    HumanOverride,
    StrategistError,
    StrategistPlanner,
    StrategyContext,
)
from app.agents.strategist import (
    StrategistPreconditionError,
    assert_preconditions,
    ensure_strategist_agent,
    propose,
)
from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import AgentKind, CampaignStatus, CampaignType, ChannelPlatform, UserRole
from app.db.models import (
    Agent,
    AppUser,
    Audience,
    AuditLog,
    Campaign,
    Channel,
    StrategyProposal,
    Tenant,
    TenantConstraint,
)
from app.db.session import set_tenant_context

_API = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# Planner — Anthropic-mocked
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


def _basic_ctx(**overrides) -> StrategyContext:
    base = StrategyContext(
        campaign_name="Q3 Launch",
        campaign_type=CampaignType.product_launch.value,
        objective="Acquire 500 MQLs in EMEA",
        brief="Beta-launch a new SMB tier",
        budget_total=Decimal("10000.00"),
        currency="USD",
        start_date="2026-06-01",
        end_date="2026-06-30",
        audience_size=1200,
        audience_summary="EMEA SMBs in software",
        available_channels=[
            ChannelInfo(platform="email", name="Lifecycle email"),
            ChannelInfo(platform="linkedin", name="LinkedIn org page"),
        ],
    )
    return StrategyContext(**{**base.__dict__, **overrides})


def _valid_payload() -> dict[str, object]:
    return {
        "channels": [
            {
                "platform": "email",
                "name": "Lifecycle email",
                "allocation_pct": 60,
                "allocation_amount": "6000.00",
                "rationale": "Strong intent signal from prior list.",
                "human_override": False,
            },
            {
                "platform": "linkedin",
                "name": "LinkedIn org page",
                "allocation_pct": 40,
                "allocation_amount": "4000.00",
                "rationale": "Where the EMEA buyer audience reads.",
                "human_override": False,
            },
        ],
        "kpis": {
            "primary": {
                "metric": "mql_count",
                "target": 500,
                "rationale": "Matches the stated objective.",
            },
            "secondary": [
                {"metric": "cpl", "target": 20, "rationale": "Half of last quarter."}
            ],
        },
        "summary_rationale": "Email-led, LinkedIn-supported plan with measurable MQL target.",
    }


def _planner() -> StrategistPlanner:
    return StrategistPlanner(
        client=AsyncAnthropic(api_key="test-key"), model="claude-sonnet-4-6"
    )


@respx.mock
async def test_planner_happy_path_returns_payload_with_no_warnings() -> None:
    respx.post(_API).mock(return_value=_anthropic_response(_valid_payload()))
    result = await _planner().propose(_basic_ctx())
    assert result.validation_warnings == []
    assert result.attempts == 1
    assert [c["platform"] for c in result.payload["channels"]] == ["email", "linkedin"]


@respx.mock
async def test_planner_retries_on_forbidden_channel_until_clean() -> None:
    bad = _valid_payload()
    bad["channels"][0]["platform"] = "sms"  # not in active channels, also forbidden
    bad["channels"][0]["allocation_pct"] = 30
    bad["channels"][1]["allocation_pct"] = 70
    bad["channels"][1]["allocation_amount"] = "7000.00"

    respx.post(_API).mock(
        side_effect=[
            _anthropic_response(bad),
            _anthropic_response(_valid_payload()),
        ]
    )
    ctx = _basic_ctx(forbidden_platforms=["sms"])
    result = await _planner().propose(ctx)
    assert result.attempts == 2
    assert result.validation_warnings == []


@respx.mock
async def test_planner_returns_best_attempt_with_warnings_when_all_retries_fail() -> None:
    bad = _valid_payload()
    bad["channels"][0]["rationale"] = ""  # always-failing violation

    respx.post(_API).mock(return_value=_anthropic_response(bad))
    result = await _planner().propose(_basic_ctx())
    assert result.attempts == 3
    assert any(v["kind"] == "missing_rationale" for v in result.validation_warnings)


@respx.mock
async def test_planner_catches_allocation_sum_violation() -> None:
    bad = _valid_payload()
    bad["channels"][0]["allocation_pct"] = 30  # 30 + 40 = 70, off by 30
    respx.post(_API).mock(return_value=_anthropic_response(bad))
    result = await _planner().propose(_basic_ctx())
    assert any(v["kind"] == "allocation_sum" for v in result.validation_warnings)


@respx.mock
async def test_planner_catches_amount_mismatch() -> None:
    bad = _valid_payload()
    bad["channels"][0]["allocation_amount"] = "1000.00"  # pct says 60 but amount says 10%
    respx.post(_API).mock(return_value=_anthropic_response(bad))
    result = await _planner().propose(_basic_ctx())
    assert any(v["kind"] == "amount_mismatch" for v in result.validation_warnings)


@respx.mock
async def test_planner_requires_overrides_to_be_present_and_flagged() -> None:
    overrides = [
        HumanOverride(
            platform="email",
            allocation_pct=Decimal("60"),
            allocation_amount=Decimal("6000.00"),
        )
    ]
    # Model drops the override entirely → should be flagged.
    dropped = _valid_payload()
    dropped["channels"] = [
        {
            "platform": "linkedin",
            "name": "LinkedIn org page",
            "allocation_pct": 100,
            "allocation_amount": "10000.00",
            "rationale": "All in on LI.",
            "human_override": False,
        }
    ]
    respx.post(_API).mock(return_value=_anthropic_response(dropped))
    result = await _planner().propose(_basic_ctx(human_overrides=overrides))
    assert any(v["kind"] == "missing_override" for v in result.validation_warnings)


@respx.mock
async def test_planner_flags_overrides_that_are_present_but_not_marked() -> None:
    overrides = [
        HumanOverride(
            platform="email",
            allocation_pct=Decimal("60"),
            allocation_amount=Decimal("6000.00"),
        )
    ]
    payload = _valid_payload()
    payload["channels"][0]["human_override"] = False  # override not flagged
    respx.post(_API).mock(return_value=_anthropic_response(payload))
    result = await _planner().propose(_basic_ctx(human_overrides=overrides))
    assert any(v["kind"] == "override_unflagged" for v in result.validation_warnings)


async def test_planner_raises_on_empty_channels_before_calling_model() -> None:
    with pytest.raises(StrategistError):
        await _planner().propose(_basic_ctx(available_channels=[]))


async def test_planner_raises_when_every_channel_is_forbidden() -> None:
    with pytest.raises(StrategistError):
        await _planner().propose(
            _basic_ctx(forbidden_platforms=["email", "linkedin"])
        )


# ---------------------------------------------------------------------------
# Agent module — DB integration
# ---------------------------------------------------------------------------


async def _seed_campaign(
    db_engine: AsyncEngine,
    *,
    tenant_name: str = "strat",
    with_audience: bool = True,
    with_channels: bool = True,
    objective: str = "Hit 500 MQLs in EMEA",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a tenant + campaign + (optional) audience + (optional) channels.
    Returns (tenant_id, campaign_id)."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"{tenant_name}-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()

        campaign = Campaign(
            tenant_id=tenant.id,
            name=f"camp-{uuid.uuid4().hex[:6]}",
            campaign_type=CampaignType.product_launch,
            objective=objective,
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            brief="A brief",
            status=CampaignStatus.audience_built,
        )
        session.add(campaign)
        await session.flush()

        if with_audience:
            session.add(
                Audience(
                    tenant_id=tenant.id,
                    campaign_id=campaign.id,
                    name="seg",
                    segment_criteria={},
                    estimated_size=100,
                    actual_size=100,
                    refreshed_at=datetime.now(UTC),
                )
            )

        if with_channels:
            for platform, name in [
                (ChannelPlatform.email, "Lifecycle email"),
                (ChannelPlatform.linkedin, "LinkedIn"),
            ]:
                session.add(
                    Channel(
                        tenant_id=tenant.id,
                        name=name,
                        platform=platform,
                        is_active=True,
                    )
                )

        await session.flush()
        return tenant.id, campaign.id


async def test_ensure_strategist_agent_is_idempotent(db_engine: AsyncEngine) -> None:
    tenant_id, _ = await _seed_campaign(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        a = await ensure_strategist_agent(session, tenant_id)
        b = await ensure_strategist_agent(session, tenant_id)
        assert a.id == b.id
        assert a.agent_type == AgentKind.campaign_strategist


async def test_assert_preconditions_rejects_missing_audience(
    db_engine: AsyncEngine,
) -> None:
    tenant_id, campaign_id = await _seed_campaign(db_engine, with_audience=False)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        with pytest.raises(StrategistPreconditionError) as exc:
            await assert_preconditions(session, campaign)
        assert "audience" in str(exc.value).lower()


async def test_assert_preconditions_rejects_empty_objective(
    db_engine: AsyncEngine,
) -> None:
    tenant_id, campaign_id = await _seed_campaign(db_engine, objective="   ")
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        with pytest.raises(StrategistPreconditionError):
            await assert_preconditions(session, campaign)


async def test_assert_preconditions_rejects_no_active_channels(
    db_engine: AsyncEngine,
) -> None:
    tenant_id, campaign_id = await _seed_campaign(db_engine, with_channels=False)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        campaign = await session.get(Campaign, campaign_id)
        with pytest.raises(StrategistPreconditionError) as exc:
            await assert_preconditions(session, campaign)
        assert "channels" in str(exc.value).lower()


@respx.mock
async def test_propose_persists_new_version_and_increments(
    db_engine: AsyncEngine,
) -> None:
    tenant_id, campaign_id = await _seed_campaign(db_engine)
    respx.post(_API).mock(return_value=_anthropic_response(_valid_payload()))

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        out1 = await propose(
            session,
            campaign_id=campaign_id,
            planner=_planner(),
            triggered_by_user_id=None,
        )
        assert out1["version"] == 1

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        out2 = await propose(
            session,
            campaign_id=campaign_id,
            planner=_planner(),
            triggered_by_user_id=None,
        )
        assert out2["version"] == 2

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(StrategyProposal).where(StrategyProposal.campaign_id == campaign_id)
            )
        ).scalars().all()
        assert sorted(r.version for r in rows) == [1, 2]
        assert all(r.created_by_kind == "agent" for r in rows)
        # the agent row should exist and own the proposals
        agent_row = (
            await session.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.agent_type == AgentKind.campaign_strategist,
                )
            )
        ).scalar_one()
        assert all(r.created_by_id == agent_row.id for r in rows)


@respx.mock
async def test_propose_carries_human_overrides_into_next_replan(
    db_engine: AsyncEngine,
) -> None:
    tenant_id, campaign_id = await _seed_campaign(db_engine)

    # First proposal with email flagged as human_override
    overridden = _valid_payload()
    overridden["channels"][0]["human_override"] = True
    respx.post(_API).mock(return_value=_anthropic_response(overridden))

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        await propose(
            session,
            campaign_id=campaign_id,
            planner=_planner(),
            triggered_by_user_id=None,
        )

    # Re-plan — model returns same payload (already honours the override).
    # We can't easily inspect the prompt here, but the validator path is
    # what ensures override-drift would be caught.
    respx.post(_API).mock(return_value=_anthropic_response(overridden))
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        out = await propose(
            session,
            campaign_id=campaign_id,
            planner=_planner(),
            triggered_by_user_id=None,
        )
        assert out["warnings_count"] == 0


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


@pytest.fixture
async def tenant_in_db(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"strat-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@strat.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def client_as(
    override_api_db,
    db_engine: AsyncEngine,
    tenant_in_db: uuid.UUID,
) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, tenant_in_db, role)
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


async def _seed_for_api(
    db_engine: AsyncEngine, tenant_id: uuid.UUID, *, with_audience: bool = True
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        campaign = Campaign(
            tenant_id=tenant_id,
            name=f"camp-{uuid.uuid4().hex[:6]}",
            campaign_type=CampaignType.product_launch,
            objective="Drive demand",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            status=CampaignStatus.audience_built,
        )
        session.add(campaign)
        await session.flush()
        if with_audience:
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
        for platform, name in [
            (ChannelPlatform.email, "Lifecycle email"),
            (ChannelPlatform.linkedin, "LinkedIn"),
        ]:
            session.add(
                Channel(tenant_id=tenant_id, name=name, platform=platform, is_active=True)
            )
        await session.flush()
        return campaign.id


async def _seed_proposal(
    db_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    *,
    version: int = 1,
    is_accepted: bool = False,
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        row = StrategyProposal(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            version=version,
            payload=_valid_payload(),
            is_accepted=is_accepted,
            created_by_kind="agent",
        )
        session.add(row)
        await session.flush()
        return row.id


async def test_enqueue_strategy_returns_503_without_anthropic_key(
    client_as, db_engine: AsyncEngine, tenant_in_db, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/campaigns/{campaign_id}/strategy")
    assert resp.status_code == 503


async def test_enqueue_strategy_returns_422_without_audience(
    client_as, db_engine: AsyncEngine, tenant_in_db, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    campaign_id = await _seed_for_api(db_engine, tenant_in_db, with_audience=False)
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/campaigns/{campaign_id}/strategy")
    assert resp.status_code == 422
    assert "audience" in resp.json()["detail"].lower()


async def test_enqueue_strategy_returns_202_when_ready(
    client_as, db_engine: AsyncEngine, tenant_in_db, monkeypatch
) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/campaigns/{campaign_id}/strategy")
    assert resp.status_code == 202
    body = resp.json()
    assert body["skill_name"] == "campaign_strategist.propose"
    assert body["status"] == "queued"


async def test_viewer_cannot_enqueue(client_as, db_engine, tenant_in_db, monkeypatch) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post(f"/api/campaigns/{campaign_id}/strategy")
    assert resp.status_code == 403


async def test_get_latest_returns_404_then_200(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    client, _ = await client_as(UserRole.viewer)

    resp = await client.get(f"/api/campaigns/{campaign_id}/strategy")
    assert resp.status_code == 404

    await _seed_proposal(db_engine, tenant_in_db, campaign_id, version=1)
    resp = await client.get(f"/api/campaigns/{campaign_id}/strategy")
    assert resp.status_code == 200
    assert resp.json()["version"] == 1


async def test_history_returns_versions_desc(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    for v in (1, 2, 3):
        await _seed_proposal(db_engine, tenant_in_db, campaign_id, version=v)

    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/campaigns/{campaign_id}/strategy/history")
    assert resp.status_code == 200
    versions = [item["version"] for item in resp.json()["items"]]
    assert versions == [3, 2, 1]


async def test_patch_overrides_marks_channel_and_writes_audit(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    proposal_id = await _seed_proposal(db_engine, tenant_in_db, campaign_id)

    client, _ = await client_as(UserRole.marketer)
    resp = await client.patch(
        f"/api/strategy-proposals/{proposal_id}",
        json={
            "channel_overrides": [
                {
                    "platform": "email",
                    "allocation_pct": 70,
                    "allocation_amount": "7000.00",
                    "human_override": True,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    email_row = next(c for c in body["payload"]["channels"] if c["platform"] == "email")
    assert email_row["human_override"] is True
    assert email_row["allocation_pct"] == 70
    assert email_row["allocation_amount"] == "7000.00"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "strategy_proposal",
                    AuditLog.entity_id == proposal_id,
                    AuditLog.action == "overridden",
                )
            )
        ).scalars().all()
        assert len(audits) == 1


async def test_patch_unknown_platform_returns_422(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    proposal_id = await _seed_proposal(db_engine, tenant_in_db, campaign_id)
    client, _ = await client_as(UserRole.marketer)
    resp = await client.patch(
        f"/api/strategy-proposals/{proposal_id}",
        json={
            "channel_overrides": [
                {"platform": "sms", "allocation_pct": 50, "human_override": True}
            ]
        },
    )
    assert resp.status_code == 422


async def test_patch_accepted_proposal_returns_409(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    proposal_id = await _seed_proposal(
        db_engine, tenant_in_db, campaign_id, is_accepted=True
    )
    client, _ = await client_as(UserRole.marketer)
    resp = await client.patch(
        f"/api/strategy-proposals/{proposal_id}",
        json={
            "channel_overrides": [
                {"platform": "email", "human_override": True}
            ]
        },
    )
    assert resp.status_code == 409


async def test_accept_flips_flag_and_transitions_state(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    proposal_id = await _seed_proposal(db_engine, tenant_in_db, campaign_id)

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/strategy-proposals/{proposal_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["is_accepted"] is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign.status == CampaignStatus.strategy_set


async def test_accept_clears_prior_accepted_proposal(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    first = await _seed_proposal(
        db_engine, tenant_in_db, campaign_id, version=1, is_accepted=True
    )
    second = await _seed_proposal(db_engine, tenant_in_db, campaign_id, version=2)

    # campaign already advanced because first was "accepted" via direct seed —
    # roll it back so the state machine can transition cleanly.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        campaign = await session.get(Campaign, campaign_id)
        campaign.status = CampaignStatus.audience_built

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/strategy-proposals/{second}/accept")
    assert resp.status_code == 200

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(StrategyProposal).where(
                    StrategyProposal.campaign_id == campaign_id
                ).order_by(StrategyProposal.version.asc())
            )
        ).scalars().all()
        accepted_flags = {r.version: r.is_accepted for r in rows}
        assert accepted_flags == {1: False, 2: True}


async def test_accept_is_idempotent(
    client_as, db_engine: AsyncEngine, tenant_in_db
) -> None:
    campaign_id = await _seed_for_api(db_engine, tenant_in_db)
    proposal_id = await _seed_proposal(
        db_engine, tenant_in_db, campaign_id, is_accepted=True
    )
    # Campaign is already at strategy_set in this synthetic setup; the endpoint
    # should return the existing row without erroring.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        campaign = await session.get(Campaign, campaign_id)
        campaign.status = CampaignStatus.strategy_set

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/strategy-proposals/{proposal_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["is_accepted"] is True
