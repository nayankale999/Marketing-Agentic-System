"""W36 — A/B significance evaluator (E09-S03).

Tests the evaluator's state-machine + throttle behaviour. We don't
re-test the statistical math here (that's covered in
test_ab_testing_tool.py); we test that:

  * A clear winner flips `status → significant` and sets `winner_id`.
  * A second call within 15 minutes is throttled (skipped).
  * A `stopped` test is left alone (AC #4).
  * Past max_runtime with no winner → `inconclusive` (AC #3).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.ab_testing.significance import EVAL_THROTTLE, evaluate_test
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    EventKind,
)
from app.db.models import (
    AbTest,
    AbTestAssignment,
    AnalyticEvent,
    Campaign,
    ContentAsset,
    DispatchAttempt,
    Tenant,
)


async def _seed_running_ab_test(
    db_engine: AsyncEngine,
    *,
    started_hours_ago: int = 1,
    max_runtime_hours: int | None = 168,
) -> dict:
    """Tenant + 2 variants + running A/B test with started_at set."""
    started_at = datetime.now(UTC) - timedelta(hours=started_hours_ago)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"sig-{uuid.uuid4().hex[:6]}")
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
                status=AssetStatus.scheduled,
                content=f"v{i}",
            )
            for i in range(2)
        ]
        session.add_all(variants)
        await session.flush()
        ab_test = AbTest(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="t",
            primary_metric="open",
            status=AbTestStatus.running,
            variant_a_id=variants[0].id,
            variant_b_id=variants[1].id,
            traffic_split={str(variants[0].id): 50, str(variants[1].id): 50},
            started_at=started_at,
            max_runtime_hours=max_runtime_hours,
        )
        session.add(ab_test)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "ab_test_id": ab_test.id,
            "variant_a_id": variants[0].id,
            "variant_b_id": variants[1].id,
        }


async def _seed_arm_outcome(
    db_engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    ab_test_id: uuid.UUID,
    variant_id: uuid.UUID,
    audience_size: int,
    successes: int,
) -> None:
    """Create `audience_size` assignments + dispatch_attempts for the
    variant and `successes` analytic_event rows linked via sg_message_id.
    """
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for i in range(audience_size):
            ext_id = f"u-{variant_id.hex[:6]}-{i}"
            sg_msg = f"sg-{variant_id.hex[:6]}-{i}"
            session.add(
                AbTestAssignment(
                    tenant_id=tenant_id,
                    ab_test_id=ab_test_id,
                    audience_external_id=ext_id,
                    variant_id=variant_id,
                )
            )
            session.add(
                DispatchAttempt(
                    tenant_id=tenant_id,
                    content_asset_id=variant_id,
                    recipient_identifier=f"{ext_id}@cust.com",
                    idempotency_key=f"k-{ext_id}",
                    provider="sendgrid",
                    provider_message_id=sg_msg,
                    status="sent",
                )
            )
            if i < successes:
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant_id,
                        event_type=EventKind.open,
                        payload={"sg_message_id": sg_msg},
                        provider_event_id=f"sg-event-{ext_id}",
                    )
                )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_evaluator_flips_to_significant_on_clear_winner(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_running_ab_test(db_engine)
    await _seed_arm_outcome(
        db_engine,
        tenant_id=world["tenant_id"],
        ab_test_id=world["ab_test_id"],
        variant_id=world["variant_a_id"],
        audience_size=500,
        successes=75,  # 15%
    )
    await _seed_arm_outcome(
        db_engine,
        tenant_id=world["tenant_id"],
        ab_test_id=world["ab_test_id"],
        variant_id=world["variant_b_id"],
        audience_size=500,
        successes=125,  # 25%
    )

    now = datetime.now(UTC)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await evaluate_test(
            session, ab_test_id=world["ab_test_id"], now=now
        )

    assert result.skipped is False
    assert result.status == AbTestStatus.significant
    assert result.winner_id == world["variant_b_id"]
    assert result.p_value is not None and result.p_value < 0.05

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        ab = await session.get(AbTest, world["ab_test_id"])
        assert ab.status == AbTestStatus.significant
        assert ab.winner_id == world["variant_b_id"]
        assert ab.confidence is not None
        assert ab.last_evaluated_at is not None


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


async def test_evaluator_throttles_within_window(db_engine: AsyncEngine) -> None:
    world = await _seed_running_ab_test(db_engine)
    # Underpowered numbers so the first call doesn't promote.
    await _seed_arm_outcome(
        db_engine,
        tenant_id=world["tenant_id"],
        ab_test_id=world["ab_test_id"],
        variant_id=world["variant_a_id"],
        audience_size=20,
        successes=3,
    )
    await _seed_arm_outcome(
        db_engine,
        tenant_id=world["tenant_id"],
        ab_test_id=world["ab_test_id"],
        variant_id=world["variant_b_id"],
        audience_size=20,
        successes=4,
    )

    now = datetime.now(UTC)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        first = await evaluate_test(session, ab_test_id=world["ab_test_id"], now=now)
    assert first.skipped is False

    # 5 minutes later — still inside the 15-min window.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        second = await evaluate_test(
            session,
            ab_test_id=world["ab_test_id"],
            now=now + timedelta(minutes=5),
        )
    assert second.skipped is True
    assert second.skip_reason == "throttled"

    # Past the window — runs again.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        third = await evaluate_test(
            session,
            ab_test_id=world["ab_test_id"],
            now=now + EVAL_THROTTLE + timedelta(seconds=1),
        )
    assert third.skipped is False


# ---------------------------------------------------------------------------
# Stopped tests are left alone (AC #4)
# ---------------------------------------------------------------------------


async def test_evaluator_skips_stopped_test(db_engine: AsyncEngine) -> None:
    world = await _seed_running_ab_test(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        ab = await session.get(AbTest, world["ab_test_id"])
        ab.status = AbTestStatus.stopped

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await evaluate_test(
            session, ab_test_id=world["ab_test_id"], now=datetime.now(UTC)
        )
    assert result.skipped is True
    assert result.skip_reason == "already_stopped"
    assert result.status == AbTestStatus.stopped


# ---------------------------------------------------------------------------
# Timeout → inconclusive (AC #3)
# ---------------------------------------------------------------------------


async def test_evaluator_marks_inconclusive_after_max_runtime(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_running_ab_test(
        db_engine, started_hours_ago=200, max_runtime_hours=168
    )
    # Underpowered so no winner emerges.
    await _seed_arm_outcome(
        db_engine,
        tenant_id=world["tenant_id"],
        ab_test_id=world["ab_test_id"],
        variant_id=world["variant_a_id"],
        audience_size=30,
        successes=5,
    )
    await _seed_arm_outcome(
        db_engine,
        tenant_id=world["tenant_id"],
        ab_test_id=world["ab_test_id"],
        variant_id=world["variant_b_id"],
        audience_size=30,
        successes=6,
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await evaluate_test(
            session, ab_test_id=world["ab_test_id"], now=datetime.now(UTC)
        )
    assert result.skipped is False
    assert result.status == AbTestStatus.inconclusive


async def test_evaluator_unsupported_metric_short_circuits(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_running_ab_test(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        ab = await session.get(AbTest, world["ab_test_id"])
        ab.primary_metric = "made_up_metric"

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        result = await evaluate_test(
            session, ab_test_id=world["ab_test_id"], now=datetime.now(UTC)
        )
    assert result.skipped is True
    assert result.skip_reason.startswith("unsupported_metric:")
