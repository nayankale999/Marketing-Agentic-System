"""W15 — Audience Targeting agent + materialisation (E04-S01..S04 baseline)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.audience_targeting import (
    ensure_audience_targeting_agent,
    materialise,
)
from app.api.app import app
from app.api.deps import get_current_user
from app.audiences.segmentation import SegmentationError
from app.db.enums import AgentKind, CampaignType, TaskStatus, UserRole
from app.db.models import (
    Agent,
    AppUser,
    Audience,
    AudienceMember,
    Campaign,
    Task,
    Tenant,
)
from app.orchestrator.handlers import register_builtin_handlers
from app.orchestrator.worker import run_once

register_builtin_handlers()


@pytest.fixture(autouse=True)
async def _clean_task_queue(db_engine: AsyncEngine):
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await session.execute(text("DELETE FROM agent_log WHERE task_id IS NOT NULL"))
        await session.execute(text("DELETE FROM task"))
    yield


@pytest.fixture
async def seeded(
    db_engine: AsyncEngine,
) -> tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID]:
    """Tenant + one campaign + one seed audience with 4 contacts.

    Returns (tenant_id, campaign_id, marketer_user, seed_audience_id).
    """
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"target-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            email=f"marketer-{uuid.uuid4().hex[:6]}@target.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=user.id,
            name=f"camp-{uuid.uuid4().hex[:6]}",
            campaign_type=CampaignType.lead_gen,
            objective="W15 demo",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
        )
        session.add(campaign)
        await session.flush()
        seed_audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seed",
            segment_criteria={"source": "csv"},
            actual_size=4,
            refreshed_at=datetime.now(UTC),
        )
        session.add(seed_audience)
        await session.flush()
        rows = [
            ("ada@example.com", {"country": "GB", "first_name": "Ada", "tags": ["vip"]}),
            ("bob@example.com", {"country": "US", "first_name": "Bob", "tags": ["vip"]}),
            ("carol@example.com", {"country": "US", "first_name": "Carol", "tags": []}),
            ("dave@example.com", {"country": "DE", "first_name": "Dave", "tags": ["blocked"]}),
        ]
        for ext_id, payload in rows:
            session.add(
                AudienceMember(
                    audience_id=seed_audience.id,
                    external_id=ext_id,
                    payload={"email": ext_id, **payload},
                    source="csv",
                    fetched_at=datetime.now(UTC),
                )
            )
        return tenant.id, campaign.id, user, seed_audience.id


# ---------------------------------------------------------------------------
# materialise() — direct unit
# ---------------------------------------------------------------------------


async def test_materialise_creates_audience_with_filtered_members(
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, campaign_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await materialise(
            session,
            campaign_id=campaign_id,
            audience_name="US VIPs",
            criteria={
                "include": [
                    {"field": "country", "op": "eq", "value": "US"},
                    {"field": "tags", "op": "has", "value": "vip"},
                ]
            },
        )

    assert result["member_count"] == 1
    audience_id = uuid.UUID(result["audience_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audience = await session.get(Audience, audience_id)
        assert audience is not None
        assert audience.name == "US VIPs"
        assert audience.actual_size == 1
        assert audience.estimated_size == 1
        assert audience.refreshed_at is not None

        members = (
            (
                await session.execute(
                    select(AudienceMember).where(AudienceMember.audience_id == audience_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(members) == 1
    member = members[0]
    assert member.external_id == "bob@example.com"
    assert member.source == "targeted"
    # Payload snapshot carried over from the seed audience.
    assert member.payload["first_name"] == "Bob"
    assert member.payload["country"] == "US"


async def test_materialise_empty_match_returns_zero_count(
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, campaign_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await materialise(
            session,
            campaign_id=campaign_id,
            audience_name="No match",
            criteria={"include": [{"field": "country", "op": "eq", "value": "XX"}]},
        )
    assert result["member_count"] == 0

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        members = (
            (
                await session.execute(
                    select(AudienceMember).where(
                        AudienceMember.audience_id == uuid.UUID(result["audience_id"])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert members == []


async def test_materialise_raises_on_bad_criteria(
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, campaign_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        with pytest.raises(SegmentationError):
            await materialise(
                session,
                campaign_id=campaign_id,
                audience_name="bad",
                criteria={"include": [{"field": "salary", "op": "eq", "value": "100"}]},
            )


# ---------------------------------------------------------------------------
# Orchestrator agent row + handler registration
# ---------------------------------------------------------------------------


async def test_ensure_agent_idempotent(
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
) -> None:
    tenant_id, _, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        a = await ensure_audience_targeting_agent(session, tenant_id)
        b = await ensure_audience_targeting_agent(session, tenant_id)
        assert a.id == b.id
        assert a.agent_type == AgentKind.audience_targeting


def test_handler_is_registered() -> None:
    from app.orchestrator.registry import get_handler

    assert get_handler("audience_targeting.materialise") is not None


# ---------------------------------------------------------------------------
# API endpoint + worker integration
# ---------------------------------------------------------------------------


@pytest.fixture
async def client_marketer(
    override_api_db,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
) -> AsyncIterator:
    _, _, user, _ = seeded
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield client
        finally:
            app.dependency_overrides.pop(get_current_user, None)


async def test_post_audiences_enqueues_task(
    client_marketer: httpx.AsyncClient,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, campaign_id, _, _ = seeded
    resp = await client_marketer.post(
        f"/api/campaigns/{campaign_id}/audiences",
        json={
            "name": "VIPs (US)",
            "criteria": {
                "include": [
                    {"field": "country", "op": "eq", "value": "US"},
                    {"field": "tags", "op": "has", "value": "vip"},
                ]
            },
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["skill_name"] == "audience_targeting.materialise"
    assert body["status"] == "queued"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(body["task_id"]))
        assert task is not None
        assert task.status == TaskStatus.queued
        assert task.input_data["audience_name"] == "VIPs (US)"
        assert task.campaign_id == campaign_id

        # Agent row was created.
        agent = (
            await session.execute(
                select(Agent).where(
                    Agent.tenant_id == task.tenant_id,
                    Agent.agent_type == AgentKind.audience_targeting,
                )
            )
        ).scalar_one()
        assert task.agent_id == agent.id


async def test_post_audiences_then_worker_materialises_end_to_end(
    client_marketer: httpx.AsyncClient,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, campaign_id, _, _ = seeded
    resp = await client_marketer.post(
        f"/api/campaigns/{campaign_id}/audiences",
        json={
            "name": "All US contacts",
            "criteria": {"include": [{"field": "country", "op": "eq", "value": "US"}]},
        },
    )
    task_id = uuid.UUID(resp.json()["task_id"])

    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    handled = await run_once(worker_id="w15-test", session_maker=session_maker)
    assert handled is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.succeeded
        audience_id = uuid.UUID(task.output_data["audience_id"])
        assert task.output_data["member_count"] == 2

        members = (
            (
                await session.execute(
                    select(AudienceMember.external_id).where(
                        AudienceMember.audience_id == audience_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(members) == ["bob@example.com", "carol@example.com"]


async def test_post_audiences_unknown_campaign_returns_404(
    client_marketer: httpx.AsyncClient,
) -> None:
    resp = await client_marketer.post(
        f"/api/campaigns/{uuid.uuid4()}/audiences",
        json={
            "name": "x",
            "criteria": {"include": [{"field": "country", "op": "eq", "value": "US"}]},
        },
    )
    assert resp.status_code == 404


async def test_post_audiences_invalid_criteria_returns_422(
    client_marketer: httpx.AsyncClient,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, campaign_id, _, _ = seeded
    resp = await client_marketer.post(
        f"/api/campaigns/{campaign_id}/audiences",
        json={
            "name": "x",
            "criteria": {"include": [{"field": "salary", "op": "eq", "value": "100"}]},
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["section"] == "include"
    assert detail["index"] == 0


async def test_post_audiences_requires_marketer(
    override_api_db,
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser, uuid.UUID],
) -> None:
    tenant_id, campaign_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        viewer = AppUser(
            tenant_id=tenant_id,
            email=f"viewer-{uuid.uuid4().hex[:6]}@target.test",
            role=UserRole.viewer,
            is_active=True,
        )
        session.add(viewer)
        await session.flush()
        await session.refresh(viewer)

    app.dependency_overrides[get_current_user] = lambda: viewer
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                f"/api/campaigns/{campaign_id}/audiences",
                json={
                    "name": "x",
                    "criteria": {"include": [{"field": "country", "op": "eq", "value": "US"}]},
                },
            )
            assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
