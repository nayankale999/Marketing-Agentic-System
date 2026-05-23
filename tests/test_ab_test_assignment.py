"""W35 — Per-recipient A/B variant assignment (E09-S02).

Unit-level coverage of `assign_variant` and `pick_variant_index`:
  * Same input → same output across calls (deterministic).
  * Race-safe insert returns whatever was actually persisted.
  * Distribution roughly matches the configured split across many keys.
  * Empty / invalid split raises a typed error.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.ab_testing.assignment import (
    AbTestAssignmentError,
    assign_variant,
    pick_variant_index,
)
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
)
from app.db.models import (
    AbTest,
    AbTestAssignment,
    Campaign,
    ContentAsset,
    Tenant,
)


async def _seed_ab_test(
    db_engine: AsyncEngine, *, split: dict[str, int] | None = None
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"asg-{uuid.uuid4().hex[:6]}")
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
        variants = [
            ContentAsset(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                asset_type=AssetType.email,
                status=AssetStatus.approved,
                content=f"v{i}",
            )
            for i in range(2)
        ]
        session.add_all(variants)
        await session.flush()
        v_ids = [v.id for v in variants]
        effective_split = split or {
            str(v_ids[0]): 50,
            str(v_ids[1]): 50,
        }
        ab_test = AbTest(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="t",
            primary_metric="open",
            status=AbTestStatus.running,
            variant_a_id=v_ids[0],
            variant_b_id=v_ids[1],
            traffic_split=effective_split,
        )
        session.add(ab_test)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "ab_test_id": ab_test.id,
            "variant_ids": v_ids,
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_pick_variant_index_is_deterministic() -> None:
    test_id = uuid.uuid4()
    split = {"a": 50, "b": 50}
    first = pick_variant_index(
        ab_test_id=test_id, audience_external_id="u-1", split=split
    )
    second = pick_variant_index(
        ab_test_id=test_id, audience_external_id="u-1", split=split
    )
    assert first == second


def test_pick_variant_index_split_is_close_to_configured() -> None:
    test_id = uuid.uuid4()
    split = {"a": 70, "b": 30}
    picks = Counter(
        pick_variant_index(
            ab_test_id=test_id,
            audience_external_id=f"u-{i}",
            split=split,
        )
        for i in range(2000)
    )
    # Allow ±3pp tolerance from configured 70/30.
    a_pct = picks["a"] / 2000 * 100
    assert 67 <= a_pct <= 73, f"a_pct={a_pct}"


def test_pick_variant_index_empty_split_raises() -> None:
    with pytest.raises(AbTestAssignmentError):
        pick_variant_index(ab_test_id=uuid.uuid4(), audience_external_id="u", split={})


def test_pick_variant_index_zero_total_raises() -> None:
    with pytest.raises(AbTestAssignmentError):
        pick_variant_index(
            ab_test_id=uuid.uuid4(),
            audience_external_id="u",
            split={"a": 0, "b": 0},
        )


# ---------------------------------------------------------------------------
# assign_variant — DB
# ---------------------------------------------------------------------------


async def test_assign_variant_returns_same_variant_on_repeat(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_ab_test(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        first = await assign_variant(
            session,
            tenant_id=world["tenant_id"],
            ab_test_id=world["ab_test_id"],
            audience_external_id="ext-1",
        )
        second = await assign_variant(
            session,
            tenant_id=world["tenant_id"],
            ab_test_id=world["ab_test_id"],
            audience_external_id="ext-1",
        )
    assert first == second
    assert first in world["variant_ids"]


async def test_assign_variant_persists_row(db_engine: AsyncEngine) -> None:
    world = await _seed_ab_test(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await assign_variant(
            session,
            tenant_id=world["tenant_id"],
            ab_test_id=world["ab_test_id"],
            audience_external_id="ext-9",
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(AbTestAssignment).where(
                    AbTestAssignment.ab_test_id == world["ab_test_id"]
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].audience_external_id == "ext-9"


async def test_assign_variant_unknown_test_raises(db_engine: AsyncEngine) -> None:
    world = await _seed_ab_test(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        with pytest.raises(AbTestAssignmentError):
            await assign_variant(
                session,
                tenant_id=world["tenant_id"],
                ab_test_id=uuid.uuid4(),
                audience_external_id="ext-x",
            )


async def test_assign_variant_distribution_matches_configured_split(
    db_engine: AsyncEngine,
) -> None:
    # Skewed split: variant A should win ~80% of assignments.
    world = await _seed_ab_test(db_engine)
    v_a, v_b = world["variant_ids"]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # Patch the test's split to 80/20.
        ab = await session.get(AbTest, world["ab_test_id"])
        ab.traffic_split = {str(v_a): 80, str(v_b): 20}
        await session.flush()

    counts = Counter()
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for i in range(500):
            assigned = await assign_variant(
                session,
                tenant_id=world["tenant_id"],
                ab_test_id=world["ab_test_id"],
                audience_external_id=f"r-{i}",
            )
            counts[assigned] += 1

    a_pct = counts[v_a] / 500 * 100
    # Allow ±5pp slack on a 500-sample test.
    assert 75 <= a_pct <= 85, f"a_pct={a_pct}, counts={dict(counts)}"
