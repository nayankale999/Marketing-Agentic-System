"""Optimisation recommendations (W37, E10-S03).

Rules-based generator for the Analytics & Optimisation agent. The rules
emit `optimisation_recommendation` rows that a marketer can accept /
reject in the UI.

What ships in W37
-----------------
  * `budget_shift` — if one channel's clicks-per-spend (or impressions-
    per-spend, when click data is sparse) is meaningfully higher than
    another's over the past 7 days, propose moving 20% of the laggard's
    allocation to the leader. Predicted uplift = delta-CTR × shifted
    spend / total spend.

Future rules (called out so the structure absorbs them cleanly)
---------------------------------------------------------------
  * `creative_swap` — when an A/B test promoted a winner mid-flight,
    swap a lagging asset on another touchpoint with similar variant
    content.
  * `schedule_change` — when open rate varies strongly by time-of-day,
    propose shifting the send window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import EventKind
from app.db.models import (
    AnalyticEvent,
    Campaign,
    ContentAsset,
    DispatchAttempt,
    OptimisationRecommendation,
    StrategyProposal,
)


# E10-S03 requires "7 days of data". We honour that as the minimum
# campaign age; rules that need more data raise their floor.
MIN_DATA_DAYS = 7

# Two-arm budget shift: if the leader's CPC (or CPI for impression-only
# channels) is at least this much better than the laggard's, recommend.
_BUDGET_SHIFT_MIN_RATIO = Decimal("1.5")
# Cap on how much of the laggard's spend we propose moving, so a single
# proposal can't redirect the whole allocation. The marketer can iterate.
_BUDGET_SHIFT_PCT = Decimal("0.20")


@dataclass(frozen=True)
class ChannelMetric:
    channel_id: UUID | None
    name: str
    spend: Decimal
    clicks: int
    impressions: int

    @property
    def click_per_spend(self) -> Decimal:
        if self.spend <= 0:
            return Decimal(0)
        return Decimal(self.clicks) / self.spend


@dataclass(frozen=True)
class ProposalBundle:
    """Convenience holder for the proposal + rationale + uplift."""

    kind: str
    proposal: dict[str, Any]
    rationale: str
    predicted_uplift: Decimal
    supporting: dict[str, Any] = field(default_factory=dict)


async def generate_recommendations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    now: datetime,
) -> list[OptimisationRecommendation]:
    """Run the W37 rule set against `campaign_id`. Returns the persisted
    recommendation rows. Existing pending rows are not duplicated — when
    a recommendation of the same kind+target already exists in `pending`,
    we skip rather than spam the marketer."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        return []

    # AC #1: need 7 days of data.
    age = now - datetime.combine(
        campaign.start_date, datetime.min.time(), tzinfo=now.tzinfo
    )
    if age < timedelta(days=MIN_DATA_DAYS):
        return []

    out: list[OptimisationRecommendation] = []

    bundle = await _try_budget_shift(
        session, tenant_id=tenant_id, campaign_id=campaign_id, now=now
    )
    if bundle is not None and not await _already_pending(
        session,
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        kind=bundle.kind,
        target=bundle.proposal,
    ):
        rec = OptimisationRecommendation(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            kind=bundle.kind,
            proposal=bundle.proposal,
            rationale=bundle.rationale,
            predicted_uplift=bundle.predicted_uplift,
            status="pending",
        )
        session.add(rec)
        await session.flush()
        out.append(rec)

    return out


# ---------------------------------------------------------------------------
# Rule: budget shift
# ---------------------------------------------------------------------------


async def _try_budget_shift(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    now: datetime,
) -> ProposalBundle | None:
    """If we can find a clearly-leading channel and a clearly-lagging
    channel, propose a 20% shift from laggard → leader."""
    cutoff = now - timedelta(days=MIN_DATA_DAYS)
    metrics = await _channel_metrics(
        session,
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        since=cutoff,
    )
    if len(metrics) < 2:
        return None

    # Filter to channels with non-zero spend AND non-zero clicks — we
    # need both to compute click_per_spend meaningfully.
    eligible = [m for m in metrics if m.spend > 0 and m.clicks > 0]
    if len(eligible) < 2:
        return None

    eligible.sort(key=lambda m: m.click_per_spend, reverse=True)
    leader = eligible[0]
    laggard = eligible[-1]
    if laggard.click_per_spend == 0:
        return None
    if leader.click_per_spend / laggard.click_per_spend < _BUDGET_SHIFT_MIN_RATIO:
        return None

    proposal_payload = await _resolve_strategy_allocations(
        session, tenant_id=tenant_id, campaign_id=campaign_id
    )
    if not proposal_payload:
        return None

    # Find the laggard's allocation; bail if it's not in the proposal.
    laggard_alloc = proposal_payload.get(laggard.name)
    leader_alloc = proposal_payload.get(leader.name)
    if laggard_alloc is None or leader_alloc is None:
        return None

    shifted_pct = float(_BUDGET_SHIFT_PCT * Decimal(laggard_alloc["allocation_pct"]))
    new_laggard_pct = laggard_alloc["allocation_pct"] - shifted_pct
    new_leader_pct = leader_alloc["allocation_pct"] + shifted_pct

    delta_cpc = leader.click_per_spend - laggard.click_per_spend
    total_spend = sum((m.spend for m in metrics), Decimal(0))
    if total_spend <= 0:
        return None
    # Predicted uplift, as fraction of total clicks: (Δcpc × shifted spend)
    # / current_total_clicks. Bound to [0, 1] so the column is happy.
    shifted_spend = laggard.spend * _BUDGET_SHIFT_PCT
    extra_clicks = delta_cpc * shifted_spend
    current_clicks = sum((Decimal(m.clicks) for m in metrics), Decimal(0))
    predicted_uplift = (
        extra_clicks / current_clicks if current_clicks > 0 else Decimal(0)
    )
    # Clamp to the column's NUMERIC(6,4) range.
    if predicted_uplift > Decimal("9.9999"):
        predicted_uplift = Decimal("9.9999")
    if predicted_uplift < Decimal(0):
        predicted_uplift = Decimal(0)

    proposal = {
        "from": {
            "channel": laggard.name,
            "allocation_pct": laggard_alloc["allocation_pct"],
            "new_allocation_pct": round(new_laggard_pct, 2),
        },
        "to": {
            "channel": leader.name,
            "allocation_pct": leader_alloc["allocation_pct"],
            "new_allocation_pct": round(new_leader_pct, 2),
        },
        "shifted_pct": round(shifted_pct, 2),
    }
    rationale = (
        f"Channel '{leader.name}' clicks-per-spend is "
        f"{leader.click_per_spend:.4f} vs '{laggard.name}' at "
        f"{laggard.click_per_spend:.4f} over the last {MIN_DATA_DAYS} days "
        f"({leader.click_per_spend / laggard.click_per_spend:.2f}× ratio). "
        f"Shifting {shifted_pct:.1f}% of '{laggard.name}'s allocation to "
        f"'{leader.name}' is projected to lift total clicks by "
        f"{float(predicted_uplift):.2%}."
    )
    return ProposalBundle(
        kind="budget_shift",
        proposal=proposal,
        rationale=rationale,
        predicted_uplift=predicted_uplift,
        supporting={
            "leader_clicks": leader.clicks,
            "leader_spend": str(leader.spend),
            "laggard_clicks": laggard.clicks,
            "laggard_spend": str(laggard.spend),
        },
    )


# ---------------------------------------------------------------------------
# Per-channel metric loader
# ---------------------------------------------------------------------------


async def _channel_metrics(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    since: datetime,
) -> list[ChannelMetric]:
    """Per-channel clicks / impressions / spend totals over the window."""
    sg_message_id = AnalyticEvent.payload["sg_message_id"].astext
    indirect_link = exists(
        select(DispatchAttempt.id)
        .join(
            ContentAsset,
            (ContentAsset.id == DispatchAttempt.content_asset_id)
            & (ContentAsset.campaign_id == campaign_id),
        )
        .where(
            DispatchAttempt.tenant_id == tenant_id,
            DispatchAttempt.provider_message_id == sg_message_id,
        )
    )

    base_filter = (
        (AnalyticEvent.tenant_id == tenant_id)
        & ((AnalyticEvent.campaign_id == campaign_id) | indirect_link)
        & (AnalyticEvent.event_at >= since)
    )

    # Counts per (channel_id, event_type). channel_id may be NULL for
    # events whose attribution path doesn't carry it; those land in the
    # "(unattributed)" bucket and aren't usable for the budget rule.
    stmt = (
        select(
            AnalyticEvent.channel_id,
            AnalyticEvent.event_type,
            func.count(AnalyticEvent.id),
            func.coalesce(func.sum(AnalyticEvent.metric_value), 0),
        )
        .where(base_filter)
        .group_by(AnalyticEvent.channel_id, AnalyticEvent.event_type)
    )
    rows = (await session.execute(stmt)).all()

    by_channel: dict[UUID | None, dict[str, Any]] = {}
    for channel_id, event_type, count, metric_sum in rows:
        bucket = by_channel.setdefault(
            channel_id,
            {"clicks": 0, "impressions": 0, "spend": Decimal(0)},
        )
        if event_type == EventKind.click:
            bucket["clicks"] += int(count)
        elif event_type == EventKind.impression:
            bucket["impressions"] += int(count)
        elif event_type == EventKind.spend:
            bucket["spend"] += Decimal(metric_sum or 0)

    out: list[ChannelMetric] = []
    for channel_id, bucket in by_channel.items():
        if channel_id is None:
            continue
        # Pull channel name lazily — small N of channels.
        from app.db.models import Channel

        name = (
            await session.execute(
                select(Channel.name).where(Channel.id == channel_id)
            )
        ).scalar_one_or_none() or str(channel_id)
        out.append(
            ChannelMetric(
                channel_id=channel_id,
                name=name,
                spend=bucket["spend"],
                clicks=bucket["clicks"],
                impressions=bucket["impressions"],
            )
        )
    return out


async def _resolve_strategy_allocations(
    session: AsyncSession, *, tenant_id: UUID, campaign_id: UUID
) -> dict[str, dict[str, Any]]:
    """Return `{channel_name: {allocation_pct, allocation_amount}}` from
    the campaign's accepted strategy proposal. Empty when no proposal
    is accepted yet."""
    proposal = (
        await session.execute(
            select(StrategyProposal)
            .where(
                StrategyProposal.tenant_id == tenant_id,
                StrategyProposal.campaign_id == campaign_id,
                StrategyProposal.is_accepted.is_(True),
            )
            .order_by(StrategyProposal.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if proposal is None:
        return {}
    payload = proposal.payload or {}
    out: dict[str, dict[str, Any]] = {}
    for c in payload.get("channels", []) or []:
        name = c.get("name")
        if not name:
            continue
        out[name] = {
            "allocation_pct": float(c.get("allocation_pct", 0)),
            "allocation_amount": c.get("allocation_amount"),
        }
    return out


async def _already_pending(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    kind: str,
    target: dict[str, Any],
) -> bool:
    """Suppress duplicate recommendations: same campaign + kind + target
    while one is still pending."""
    from_channel = (target.get("from") or {}).get("channel")
    to_channel = (target.get("to") or {}).get("channel")
    rows = (
        await session.execute(
            select(OptimisationRecommendation).where(
                OptimisationRecommendation.tenant_id == tenant_id,
                OptimisationRecommendation.campaign_id == campaign_id,
                OptimisationRecommendation.kind == kind,
                OptimisationRecommendation.status == "pending",
            )
        )
    ).scalars().all()
    for r in rows:
        rfrom = (r.proposal.get("from") or {}).get("channel")
        rto = (r.proposal.get("to") or {}).get("channel")
        if rfrom == from_channel and rto == to_channel:
            return True
    return False


__all__ = ["generate_recommendations", "MIN_DATA_DAYS"]
