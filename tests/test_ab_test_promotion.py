"""W36 — A/B test promotion (E09-S05).

Tests for `POST /api/ab-tests/{id}/promote`:
  * AC #1: traffic_split flips to 100% on winner; losers → archived.
  * No-winner / wrong-status → 409.
  * AC #4: compliance-flagged winner → 409 for marketer, but a manager
    can still promote.
  * Audit row captures prior split + violations.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    UserRole,
)
from app.db.models import (
    AbTest,
    AppUser,
    AuditLog,
    Campaign,
    ContentAsset,
    Tenant,
)


async def _seed_significant(
    db_engine: AsyncEngine,
    *,
    winner_has_compliance_flag: bool = False,
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"prom-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("0"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today(),
            brief="b",
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        variants = []
        for i in range(2):
            meta: dict = {}
            v = ContentAsset(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                asset_type=AssetType.email,
                status=AssetStatus.scheduled,
                content=f"v{i}",
                extra_metadata=meta,
            )
            session.add(v)
            await session.flush()
            variants.append(v)
        winner = variants[1]
        if winner_has_compliance_flag:
            winner.extra_metadata = {
                "compliance_violations": [
                    {"rule": "no-guarantees", "severity": "blocker"}
                ]
            }
        # Link the family via ab_test_group_id so _list_variants finds them.
        ab_test = AbTest(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="t",
            primary_metric="open",
            status=AbTestStatus.significant,
            variant_a_id=variants[0].id,
            variant_b_id=winner.id,
            winner_id=winner.id,
            traffic_split={str(variants[0].id): 50, str(winner.id): 50},
            confidence=Decimal("0.9500"),
        )
        session.add(ab_test)
        await session.flush()
        for v in variants:
            v.extra_metadata = {
                **(v.extra_metadata or {}),
                "ab_test_group_id": str(ab_test.id),
            }
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "ab_test_id": ab_test.id,
            "loser_id": variants[0].id,
            "winner_id": winner.id,
        }


@pytest.fixture
async def world(override_api_db, db_engine: AsyncEngine) -> dict:
    return await _seed_significant(db_engine)


@pytest.fixture
async def client_as(world, db_engine: AsyncEngine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            user = AppUser(
                tenant_id=world["tenant_id"],
                email=f"{role.value}-{uuid.uuid4().hex[:6]}@prom.test",
                role=role,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
        app.dependency_overrides[get_current_user] = lambda: user
        transport = httpx.ASGITransport(app=app)
        c = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c, user

    try:
        yield _factory
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_promote_flips_split_and_archives_loser(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/ab-tests/{world['ab_test_id']}/promote")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["traffic_split"] == {str(world["winner_id"]): 100}

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        loser = await session.get(ContentAsset, world["loser_id"])
        assert loser.status == AssetStatus.archived
        winner = await session.get(ContentAsset, world["winner_id"])
        # Winner is left on its natural lifecycle column (scheduled in
        # the seed) — promotion doesn't move it.
        assert winner.status == AssetStatus.scheduled


async def test_promote_writes_audit_row_with_prior_split(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.marketer)
    await client.post(f"/api/ab-tests/{world['ab_test_id']}/promote")
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "ab_test",
                    AuditLog.entity_id == world["ab_test_id"],
                    AuditLog.action == "ab_test_promoted",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        meta = audits[0].extra_metadata
        assert meta["winner_id"] == str(world["winner_id"])
        assert meta["prior_split"] == {
            str(world["loser_id"]): 50,
            str(world["winner_id"]): 50,
        }


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def test_promote_rejected_when_no_winner(
    client_as, world, db_engine: AsyncEngine
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        ab = await session.get(AbTest, world["ab_test_id"])
        ab.status = AbTestStatus.running
        ab.winner_id = None

    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/ab-tests/{world['ab_test_id']}/promote")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "no_winner"


async def test_promote_returns_404_for_unknown_test(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(f"/api/ab-tests/{uuid.uuid4()}/promote")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Compliance gate (AC #4)
# ---------------------------------------------------------------------------


async def test_marketer_cannot_promote_flagged_winner(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_significant(db_engine, winner_has_compliance_flag=True)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=world["tenant_id"],
            email=f"m-{uuid.uuid4().hex[:6]}@prom.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/ab-tests/{world['ab_test_id']}/promote"
            )
            assert resp.status_code == 409
            assert resp.json()["detail"]["reason"] == "compliance_review_required"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_manager_can_promote_flagged_winner(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_significant(db_engine, winner_has_compliance_flag=True)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=world["tenant_id"],
            email=f"mgr-{uuid.uuid4().hex[:6]}@prom.test",
            role=UserRole.manager,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/ab-tests/{world['ab_test_id']}/promote"
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["traffic_split"] == {str(world["winner_id"]): 100}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
