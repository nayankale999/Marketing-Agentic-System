"""Spend ingest + reconciliation (W41, E10-S06).

Two responsibilities:

  * `ingest_platform_spend` — nightly hook (callable from the ad-platform
    connectors when they land). Updates `campaign_channel_budget.spent`
    per channel for one campaign. Refuses to touch `spent` if the
    campaign is `completed` AND has a `matched` reconciliation for the
    latest period (E10-S06 AC #4 read-only state).

  * `run_reconciliation` — monthly hook. Computes committed (= sum of
    `campaign_channel_budget.spent` for the campaign) vs invoiced (from
    the caller-provided dict, which would come from a parsed invoice
    file in a real pipeline). Writes one `spend_reconciliation` row per
    campaign and flags delta > 1% as `pending`; <= 1% lands as `matched`.

`mark_explained` / `mark_disputed` move pending/matched rows through
the admin workflow (AC #3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import CampaignStatus
from app.db.models import (
    Campaign,
    CampaignChannelBudget,
    SpendReconciliation,
)


# Delta threshold for flagging: > 1% mismatch is `pending`, <= 1% is `matched`.
MATCH_THRESHOLD_PCT = Decimal("1.00")


# ---------------------------------------------------------------------------
# Spend ingest
# ---------------------------------------------------------------------------


class SpendReadOnlyError(Exception):
    """Raised when ingest is attempted on a completed, matched campaign."""


@dataclass(frozen=True)
class ChannelSpend:
    channel_id: UUID
    amount: Decimal


async def ingest_platform_spend(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    records: list[ChannelSpend],
) -> None:
    """Upsert `campaign_channel_budget.spent` for each (campaign, channel)
    pair. Idempotent — re-ingesting the same window replaces the value."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise LookupError(f"campaign {campaign_id} not found")

    if await _is_spend_read_only(session, campaign=campaign):
        raise SpendReadOnlyError(
            f"campaign {campaign_id} is completed + reconciled — spend is read-only"
        )

    for record in records:
        stmt = (
            pg_insert(CampaignChannelBudget)
            .values(
                campaign_id=campaign_id,
                channel_id=record.channel_id,
                allocated=Decimal(0),
                spent=record.amount,
            )
            .on_conflict_do_update(
                index_elements=[
                    CampaignChannelBudget.campaign_id,
                    CampaignChannelBudget.channel_id,
                ],
                set_={"spent": record.amount},
            )
        )
        await session.execute(stmt)


async def _is_spend_read_only(
    session: AsyncSession, *, campaign: Campaign
) -> bool:
    """E10-S06 AC #4: spend on a completed + reconciled campaign is
    read-only. We check the latest reconciliation row's status — only
    `matched` locks it; `disputed` and `explained` still allow late
    corrections."""
    if campaign.status != CampaignStatus.completed:
        return False
    latest = (
        await session.execute(
            select(SpendReconciliation)
            .where(SpendReconciliation.campaign_id == campaign.id)
            .order_by(SpendReconciliation.period_end.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return latest is not None and latest.status == "matched"


# ---------------------------------------------------------------------------
# Reconciliation runner
# ---------------------------------------------------------------------------


async def run_reconciliation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    invoices: Mapping[UUID, Decimal],
) -> list[SpendReconciliation]:
    """For each campaign in `invoices`, compute committed vs invoiced and
    upsert a `spend_reconciliation` row.

    `invoices` is `{campaign_id: invoiced_amount}` — the caller is
    responsible for parsing the platform's invoice file/email into this
    shape. Returns the persisted rows."""
    out: list[SpendReconciliation] = []
    for campaign_id, invoiced in invoices.items():
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None or campaign.tenant_id != tenant_id:
            continue

        committed = await _committed_total(
            session, campaign_id=campaign_id
        )
        delta_pct = _delta_pct(committed=committed, invoiced=invoiced)
        status = "matched" if abs(delta_pct) <= MATCH_THRESHOLD_PCT else "pending"

        row = await _upsert_reconciliation(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            period_start=period_start,
            period_end=period_end,
            committed=committed,
            invoiced=invoiced,
            delta_pct=delta_pct,
            status=status,
        )
        out.append(row)
    return out


async def _committed_total(
    session: AsyncSession, *, campaign_id: UUID
) -> Decimal:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(CampaignChannelBudget.spent), 0)).where(
                CampaignChannelBudget.campaign_id == campaign_id
            )
        )
    ).scalar_one()
    return Decimal(total or 0)


def _delta_pct(*, committed: Decimal, invoiced: Decimal) -> Decimal:
    """Signed delta as a percentage of committed: positive when the
    invoice came in higher than what MAS tracked. When committed is 0,
    a non-zero invoice is treated as a 100% delta; both-zero is 0%."""
    if committed == 0:
        return Decimal("100.0000") if invoiced != 0 else Decimal("0.0000")
    raw = (invoiced - committed) / committed * Decimal(100)
    return raw.quantize(Decimal("0.0001"))


async def _upsert_reconciliation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    period_start: date,
    period_end: date,
    committed: Decimal,
    invoiced: Decimal,
    delta_pct: Decimal,
    status: str,
) -> SpendReconciliation:
    # First try insert; on the unique-conflict path, update in place so a
    # re-run of the same period refreshes the numbers.
    stmt = (
        pg_insert(SpendReconciliation)
        .values(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            period_start=period_start,
            period_end=period_end,
            committed_amount=committed,
            invoiced_amount=invoiced,
            delta_pct=delta_pct,
            status=status,
        )
        .on_conflict_do_update(
            constraint="uq_spend_reconciliation_period",
            set_={
                "committed_amount": committed,
                "invoiced_amount": invoiced,
                "delta_pct": delta_pct,
                "status": status,
            },
        )
        .returning(SpendReconciliation.id)
    )
    inserted_id = (await session.execute(stmt)).scalar_one()
    return await session.get(SpendReconciliation, inserted_id)


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------


async def mark_explained(
    session: AsyncSession,
    *,
    reconciliation_id: UUID,
    user_id: UUID,
    note: str,
    now: datetime,
) -> SpendReconciliation:
    row = await session.get(SpendReconciliation, reconciliation_id)
    if row is None:
        raise LookupError(f"spend_reconciliation {reconciliation_id} not found")
    row.status = "explained"
    row.note = note
    row.resolved_at = now
    row.resolved_by = user_id
    await session.flush()
    return row


async def mark_disputed(
    session: AsyncSession,
    *,
    reconciliation_id: UUID,
    user_id: UUID,
    note: str | None,
    now: datetime,
) -> SpendReconciliation:
    row = await session.get(SpendReconciliation, reconciliation_id)
    if row is None:
        raise LookupError(f"spend_reconciliation {reconciliation_id} not found")
    row.status = "disputed"
    row.note = note
    row.resolved_at = now
    row.resolved_by = user_id
    await session.flush()
    return row


__all__ = [
    "ChannelSpend",
    "SpendReadOnlyError",
    "MATCH_THRESHOLD_PCT",
    "ingest_platform_spend",
    "run_reconciliation",
    "mark_explained",
    "mark_disputed",
]
