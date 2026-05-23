"""W41 — Custom KPI evaluator (E10-S07).

Covers the formula language + AC #3 null-on-missing-event behavior.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.custom_kpis import evaluate_custom_kpi
from app.db.enums import (
    CampaignStatus,
    CampaignType,
    EventKind,
    UserRole,
)
from app.db.models import (
    AnalyticEvent,
    AppUser,
    Campaign,
    CustomKpi,
    Tenant,
)


async def _seed_world(db_engine: AsyncEngine) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ck-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@ck.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("0"),
            currency="USD",
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
            brief="b",
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        return {"tenant_id": tenant.id, "campaign_id": campaign.id}


async def _insert_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    event_type: EventKind,
    payload: dict | None = None,
    event_at: datetime | None = None,
) -> None:
    session.add(
        AnalyticEvent(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            event_type=event_type,
            payload=payload or {},
            provider_event_id=f"e-{uuid.uuid4().hex[:10]}",
            event_at=event_at or datetime.now(UTC),
        )
    )


def _kpi(tenant_id: uuid.UUID, formula: dict, *, name: str = "demo_clicks") -> CustomKpi:
    return CustomKpi(
        tenant_id=tenant_id,
        campaign_id=None,
        name=name,
        formula=formula,
    )


# ---------------------------------------------------------------------------
# AC #3: missing event → null
# ---------------------------------------------------------------------------


async def test_missing_event_returns_null(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        kpi = _kpi(world["tenant_id"], {"event_type": "click"})
        result = await evaluate_custom_kpi(
            session,
            kpi=kpi,
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert result.value is None
    assert result.missing_event is True
    assert "no 'click' events" in (result.message or "")


async def test_unknown_event_type_returns_null(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        kpi = _kpi(world["tenant_id"], {"event_type": "not_a_real_kind"})
        result = await evaluate_custom_kpi(
            session,
            kpi=kpi,
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert result.value is None
    assert result.missing_event is True


# ---------------------------------------------------------------------------
# Counting + filtering
# ---------------------------------------------------------------------------


async def test_counts_events_of_kind(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for _ in range(5):
            await _insert_event(
                session,
                tenant_id=world["tenant_id"],
                campaign_id=world["campaign_id"],
                event_type=EventKind.click,
                payload={"utm_content": "demo"},
            )
        await session.flush()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        kpi = _kpi(world["tenant_id"], {"event_type": "click"})
        result = await evaluate_custom_kpi(
            session,
            kpi=kpi,
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert result.value == 5
    assert result.missing_event is False


async def test_payload_filter_eq_narrows_subset(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for utm in ("demo", "demo", "demo", "other", "other"):
            await _insert_event(
                session,
                tenant_id=world["tenant_id"],
                campaign_id=world["campaign_id"],
                event_type=EventKind.click,
                payload={"utm_content": utm},
            )
        await session.flush()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        kpi = _kpi(
            world["tenant_id"],
            {
                "event_type": "click",
                "filters": [
                    {"path": "payload.utm_content", "op": "eq", "value": "demo"}
                ],
            },
        )
        result = await evaluate_custom_kpi(
            session,
            kpi=kpi,
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert result.value == 3


async def test_payload_filter_in_works(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for src in ("email", "li", "li", "facebook", "twitter"):
            await _insert_event(
                session,
                tenant_id=world["tenant_id"],
                campaign_id=world["campaign_id"],
                event_type=EventKind.click,
                payload={"utm_source": src},
            )
        await session.flush()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        kpi = _kpi(
            world["tenant_id"],
            {
                "event_type": "click",
                "filters": [
                    {
                        "path": "payload.utm_source",
                        "op": "in",
                        "value": ["email", "li"],
                    }
                ],
            },
        )
        result = await evaluate_custom_kpi(
            session,
            kpi=kpi,
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert result.value == 3


async def test_window_days_filter(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    now = datetime.now(UTC)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # 3 recent, 2 ancient
        for delta in (1, 1, 1, 30, 60):
            await _insert_event(
                session,
                tenant_id=world["tenant_id"],
                campaign_id=world["campaign_id"],
                event_type=EventKind.click,
                event_at=now - timedelta(days=delta),
            )
        await session.flush()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        kpi = _kpi(world["tenant_id"], {"event_type": "click", "window_days": 7})
        result = await evaluate_custom_kpi(
            session,
            kpi=kpi,
            campaign_id=world["campaign_id"],
            now=now,
        )
    assert result.value == 3


async def test_zero_match_is_not_missing_event(db_engine: AsyncEngine) -> None:
    """Events of the kind exist, but the filter narrows to zero. AC #3:
    that's `value=0`, not `missing_event=True`."""
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await _insert_event(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            event_type=EventKind.click,
            payload={"utm_content": "other"},
        )
        await session.flush()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        kpi = _kpi(
            world["tenant_id"],
            {
                "event_type": "click",
                "filters": [
                    {"path": "payload.utm_content", "op": "eq", "value": "demo"}
                ],
            },
        )
        result = await evaluate_custom_kpi(
            session,
            kpi=kpi,
            campaign_id=world["campaign_id"],
            now=datetime.now(UTC),
        )
    assert result.value == 0
    assert result.missing_event is False
