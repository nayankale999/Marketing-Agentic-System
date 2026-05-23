"""W41 — Spend reconciliation (E10-S06).

Covers:
  * Ingest updates campaign_channel_budget.spent
  * Ingest refuses to write when campaign is completed + matched
  * Reconciliation flags > 1% delta as `pending`
  * <= 1% delta lands as `matched`
  * mark_explained / mark_disputed lifecycle
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.analytics.spend_reconciliation import (
    ChannelSpend,
    SpendReadOnlyError,
    ingest_platform_spend,
    mark_disputed,
    mark_explained,
    run_reconciliation,
)
from app.db.enums import (
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AppUser,
    Campaign,
    CampaignChannelBudget,
    Channel,
    SpendReconciliation,
    Tenant,
)


async def _seed_world(
    db_engine: AsyncEngine, *, status: CampaignStatus = CampaignStatus.live
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"sr-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@sr.test",
            role=UserRole.admin,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        ch = Channel(
            tenant_id=tenant.id,
            name="LinkedIn",
            platform=ChannelPlatform.linkedin,
            is_active=True,
        )
        session.add(ch)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="c",
            campaign_type=CampaignType.product_launch,
            objective="o",
            budget_total=Decimal("1000"),
            currency="USD",
            start_date=date.today() - timedelta(days=30),
            end_date=date.today(),
            brief="b",
            status=status,
        )
        session.add(campaign)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "channel_id": ch.id,
            "admin_id": owner.id,
        }


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


async def test_ingest_upserts_spent(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await ingest_platform_spend(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            records=[ChannelSpend(channel_id=world["channel_id"], amount=Decimal("420.50"))],
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(
                select(CampaignChannelBudget).where(
                    CampaignChannelBudget.campaign_id == world["campaign_id"]
                )
            )
        ).scalar_one()
    assert row.spent == Decimal("420.50")


async def test_ingest_idempotent_replaces_amount(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await ingest_platform_spend(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            records=[ChannelSpend(channel_id=world["channel_id"], amount=Decimal("100"))],
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await ingest_platform_spend(
            session,
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            records=[ChannelSpend(channel_id=world["channel_id"], amount=Decimal("250"))],
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(CampaignChannelBudget).where(
                    CampaignChannelBudget.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].spent == Decimal("250")


async def test_ingest_blocked_when_completed_and_matched(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine, status=CampaignStatus.completed)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            SpendReconciliation(
                tenant_id=world["tenant_id"],
                campaign_id=world["campaign_id"],
                period_start=date.today() - timedelta(days=30),
                period_end=date.today(),
                committed_amount=Decimal("100"),
                invoiced_amount=Decimal("100"),
                delta_pct=Decimal("0"),
                status="matched",
            )
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        with pytest.raises(SpendReadOnlyError):
            await ingest_platform_spend(
                session,
                tenant_id=world["tenant_id"],
                campaign_id=world["campaign_id"],
                records=[ChannelSpend(channel_id=world["channel_id"], amount=Decimal("999"))],
            )


# ---------------------------------------------------------------------------
# Reconciliation runner
# ---------------------------------------------------------------------------


async def test_reconciliation_flags_above_one_percent_as_pending(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            CampaignChannelBudget(
                campaign_id=world["campaign_id"],
                channel_id=world["channel_id"],
                allocated=Decimal("1000"),
                spent=Decimal("500.00"),
            )
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        rows = await run_reconciliation(
            session,
            tenant_id=world["tenant_id"],
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            invoices={world["campaign_id"]: Decimal("525.00")},
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "pending"
    assert row.delta_pct == Decimal("5.0000")
    assert row.committed_amount == Decimal("500.00")
    assert row.invoiced_amount == Decimal("525.00")


async def test_reconciliation_within_one_percent_is_matched(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            CampaignChannelBudget(
                campaign_id=world["campaign_id"],
                channel_id=world["channel_id"],
                allocated=Decimal("1000"),
                spent=Decimal("500.00"),
            )
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        rows = await run_reconciliation(
            session,
            tenant_id=world["tenant_id"],
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            # 0.6% delta
            invoices={world["campaign_id"]: Decimal("503.00")},
        )
    assert rows[0].status == "matched"


async def test_rerun_replaces_row(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            CampaignChannelBudget(
                campaign_id=world["campaign_id"],
                channel_id=world["channel_id"],
                allocated=Decimal("1000"),
                spent=Decimal("500.00"),
            )
        )

    period_start = date.today() - timedelta(days=30)
    period_end = date.today()
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await run_reconciliation(
            session,
            tenant_id=world["tenant_id"],
            period_start=period_start,
            period_end=period_end,
            invoices={world["campaign_id"]: Decimal("525")},
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await run_reconciliation(
            session,
            tenant_id=world["tenant_id"],
            period_start=period_start,
            period_end=period_end,
            invoices={world["campaign_id"]: Decimal("503")},
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(SpendReconciliation).where(
                    SpendReconciliation.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "matched"
    assert rows[0].invoiced_amount == Decimal("503.00")


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------


async def test_mark_explained_sets_note_and_user(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recon = SpendReconciliation(
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            committed_amount=Decimal("100"),
            invoiced_amount=Decimal("150"),
            delta_pct=Decimal("50"),
            status="pending",
        )
        session.add(recon)
        await session.flush()
        recon_id = recon.id

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        updated = await mark_explained(
            session,
            reconciliation_id=recon_id,
            user_id=world["admin_id"],
            note="Late ad-buy from partner agency",
            now=datetime.now(UTC),
        )
    assert updated.status == "explained"
    assert updated.note == "Late ad-buy from partner agency"
    assert updated.resolved_by == world["admin_id"]


async def test_mark_disputed_lifecycle(db_engine: AsyncEngine) -> None:
    world = await _seed_world(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        recon = SpendReconciliation(
            tenant_id=world["tenant_id"],
            campaign_id=world["campaign_id"],
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            committed_amount=Decimal("100"),
            invoiced_amount=Decimal("80"),
            delta_pct=Decimal("-20"),
            status="pending",
        )
        session.add(recon)
        await session.flush()
        recon_id = recon.id

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        updated = await mark_disputed(
            session,
            reconciliation_id=recon_id,
            user_id=world["admin_id"],
            note=None,
            now=datetime.now(UTC),
        )
    assert updated.status == "disputed"
    assert updated.resolved_by == world["admin_id"]
