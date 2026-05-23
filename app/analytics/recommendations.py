"""Optimisation recommendations (W37, E10-S03; W39, E10-S05).

Rules-based generator for the Analytics & Optimisation agent. The rules
emit `optimisation_recommendation` rows that a marketer can accept /
reject in the UI.

What ships
----------
  * `budget_shift` (W39 refines W37) — compute **cost-per-outcome**
    (conversions if any, else clicks) per channel. If the leader's CPO
    is materially better than the laggard's, propose moving 30% of the
    laggard's allocation, clamped to [10%, 50%]. Honors per-channel
    `min_daily_spend` if set on `channel.api_config` (clamping the
    shift; if the clamp pushes it below 10%, drop the proposal). The
    proposal carries a confidence label ("low" / "medium" / "high")
    derived from the CPO ratio.

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


# E10-S05 AC #1: need at least 5 days of data for the budget rule.
# Other rules can raise their floor; the constant is the minimum any
# rule honours.
MIN_DATA_DAYS = 5

# Budget-shift rule (W39, E10-S05).
# Default shift sizing: 30% of the laggard's allocation. Clamped to a
# [floor, ceiling] band so a single proposal is meaningful but won't
# redirect the entire allocation.
_BUDGET_SHIFT_DEFAULT_PCT = Decimal("0.30")
_BUDGET_SHIFT_FLOOR_PCT = Decimal("0.10")   # AC #1: shifts > 10% only
_BUDGET_SHIFT_CEILING_PCT = Decimal("0.50")

# CPO ratio thresholds → confidence label. Below the min ratio we don't
# propose at all.
_BUDGET_SHIFT_MIN_RATIO = Decimal("1.2")
_CONFIDENCE_HIGH_RATIO = Decimal("2.0")
_CONFIDENCE_MEDIUM_RATIO = Decimal("1.5")


@dataclass(frozen=True)
class ChannelMetric:
    channel_id: UUID | None
    name: str
    spend: Decimal
    clicks: int
    impressions: int
    conversions: int
    min_daily_spend: Decimal | None  # from channel.api_config, if set

    def cost_per_outcome(self, *, outcome: str) -> Decimal:
        """`outcome ∈ {"conversion", "click"}`. Returns 0 when the channel
        has no spend or no outcomes — the caller filters those out."""
        if self.spend <= 0:
            return Decimal(0)
        count = self.conversions if outcome == "conversion" else self.clicks
        if count <= 0:
            return Decimal(0)
        return self.spend / Decimal(count)


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
    """W39 (E10-S05): find a leader + laggard channel by cost-per-outcome,
    propose a 30%-of-laggard shift clamped to [10%, 50%] and to any
    per-channel min_daily_spend floor.
    """
    cutoff = now - timedelta(days=MIN_DATA_DAYS)
    metrics = await _channel_metrics(
        session,
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        since=cutoff,
    )
    if len(metrics) < 2:
        return None

    # Pick the outcome that has data across the campaign. Conversions
    # are the canonical "outcome" for E10-S05; fall back to clicks when
    # the campaign hasn't logged any conversions anywhere yet.
    total_conversions = sum(m.conversions for m in metrics)
    outcome = "conversion" if total_conversions > 0 else "click"

    eligible = [
        m for m in metrics
        if m.spend > 0 and (m.conversions if outcome == "conversion" else m.clicks) > 0
    ]
    if len(eligible) < 2:
        return None

    eligible.sort(key=lambda m: m.cost_per_outcome(outcome=outcome))
    leader = eligible[0]   # lowest CPO = best
    laggard = eligible[-1]  # highest CPO = worst
    leader_cpo = leader.cost_per_outcome(outcome=outcome)
    laggard_cpo = laggard.cost_per_outcome(outcome=outcome)
    if leader_cpo <= 0:
        return None
    ratio = laggard_cpo / leader_cpo
    if ratio < _BUDGET_SHIFT_MIN_RATIO:
        return None

    confidence = _confidence_label(ratio)

    allocations = await _resolve_strategy_allocations(
        session, tenant_id=tenant_id, campaign_id=campaign_id
    )
    if not allocations:
        return None
    laggard_alloc = allocations.get(laggard.name)
    leader_alloc = allocations.get(leader.name)
    if laggard_alloc is None or leader_alloc is None:
        return None

    laggard_alloc_amount = _as_decimal(laggard_alloc.get("allocation_amount"))
    if laggard_alloc_amount is None or laggard_alloc_amount <= 0:
        return None

    # Default shift = 30% of laggard's allocation, clamped to the band.
    shift_pct = _BUDGET_SHIFT_DEFAULT_PCT
    proposed_amount = laggard_alloc_amount * shift_pct

    # AC #4: honour any per-channel minimum daily spend floor on the
    # laggard. We can't drop it below floor over the remaining window.
    floor_note: str | None = None
    if laggard.min_daily_spend is not None and laggard.min_daily_spend > 0:
        # Conservative floor: don't reduce laggard's allocation below
        # min_daily_spend * remaining_days. For MVP we just clamp to
        # `min_daily_spend` (assumes >=1 day remaining); a fuller
        # remaining-window calculation is a polish unit.
        max_shift_amount = laggard_alloc_amount - laggard.min_daily_spend
        if max_shift_amount < proposed_amount:
            proposed_amount = max(max_shift_amount, Decimal(0))
            floor_note = (
                f" Shift clamped to keep '{laggard.name}' above its "
                f"minimum daily spend floor of {laggard.min_daily_spend}."
            )

    # Drop proposals that don't move enough to matter.
    shift_pct_actual = (
        proposed_amount / laggard_alloc_amount if laggard_alloc_amount > 0 else Decimal(0)
    )
    if shift_pct_actual < _BUDGET_SHIFT_FLOOR_PCT:
        return None
    if shift_pct_actual > _BUDGET_SHIFT_CEILING_PCT:
        proposed_amount = laggard_alloc_amount * _BUDGET_SHIFT_CEILING_PCT
        shift_pct_actual = _BUDGET_SHIFT_CEILING_PCT

    leader_alloc_amount = _as_decimal(leader_alloc.get("allocation_amount")) or Decimal(0)
    new_laggard_amount = laggard_alloc_amount - proposed_amount
    new_leader_amount = leader_alloc_amount + proposed_amount

    delta_cpo = laggard_cpo - leader_cpo  # > 0
    # Predicted uplift in outcomes: (Δcpo applied to shifted spend) over
    # current outcomes — same semantic as W37 but using the CPO frame.
    current_outcomes = sum(
        (Decimal(m.conversions if outcome == "conversion" else m.clicks) for m in metrics),
        Decimal(0),
    )
    extra_outcomes = (proposed_amount / leader_cpo) - (proposed_amount / laggard_cpo)
    predicted_uplift = (
        extra_outcomes / current_outcomes if current_outcomes > 0 else Decimal(0)
    )
    if predicted_uplift > Decimal("9.9999"):
        predicted_uplift = Decimal("9.9999")
    if predicted_uplift < Decimal(0):
        predicted_uplift = Decimal(0)

    proposal = {
        "from": {
            "channel": laggard.name,
            "channel_id": str(laggard.channel_id) if laggard.channel_id else None,
            "allocation_pct": laggard_alloc["allocation_pct"],
            "allocation_amount": str(laggard_alloc_amount),
            "new_allocation_amount": str(new_laggard_amount.quantize(Decimal("0.01"))),
        },
        "to": {
            "channel": leader.name,
            "channel_id": str(leader.channel_id) if leader.channel_id else None,
            "allocation_pct": leader_alloc["allocation_pct"],
            "allocation_amount": str(leader_alloc_amount),
            "new_allocation_amount": str(new_leader_amount.quantize(Decimal("0.01"))),
        },
        "proposed_amount": str(proposed_amount.quantize(Decimal("0.01"))),
        "shift_pct": float(round(shift_pct_actual * 100, 2)),
        "confidence": confidence,
        "outcome": outcome,
        "clamped_to_floor": floor_note is not None,
    }
    rationale = (
        f"Channel '{leader.name}' cost-per-{outcome} is "
        f"{leader_cpo:.4f} vs '{laggard.name}' at {laggard_cpo:.4f} over "
        f"the last {MIN_DATA_DAYS} days ({ratio:.2f}× ratio, confidence: "
        f"{confidence}). Shifting "
        f"{proposed_amount.quantize(Decimal('0.01'))} of '{laggard.name}'s "
        f"budget to '{leader.name}' is projected to lift total "
        f"{outcome}s by {float(predicted_uplift):.2%}."
        f"{floor_note or ''}"
    )
    return ProposalBundle(
        kind="budget_shift",
        proposal=proposal,
        rationale=rationale,
        predicted_uplift=predicted_uplift,
        supporting={
            "leader_outcomes": leader.conversions if outcome == "conversion" else leader.clicks,
            "leader_spend": str(leader.spend),
            "laggard_outcomes": laggard.conversions if outcome == "conversion" else laggard.clicks,
            "laggard_spend": str(laggard.spend),
            "ratio": str(ratio.quantize(Decimal("0.0001"))),
        },
    )


def _confidence_label(ratio: Decimal) -> str:
    if ratio >= _CONFIDENCE_HIGH_RATIO:
        return "high"
    if ratio >= _CONFIDENCE_MEDIUM_RATIO:
        return "medium"
    return "low"


def _as_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


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
            {
                "clicks": 0,
                "impressions": 0,
                "conversions": 0,
                "spend": Decimal(0),
            },
        )
        if event_type == EventKind.click:
            bucket["clicks"] += int(count)
        elif event_type == EventKind.impression:
            bucket["impressions"] += int(count)
        elif event_type == EventKind.conversion:
            bucket["conversions"] += int(count)
        elif event_type == EventKind.spend:
            bucket["spend"] += Decimal(metric_sum or 0)

    out: list[ChannelMetric] = []
    for channel_id, bucket in by_channel.items():
        if channel_id is None:
            continue
        from app.db.models import Channel

        channel = (
            await session.execute(
                select(Channel).where(Channel.id == channel_id)
            )
        ).scalar_one_or_none()
        name = (channel.name if channel is not None else None) or str(channel_id)
        min_daily_spend: Decimal | None = None
        if channel is not None:
            raw = (channel.api_config or {}).get("min_daily_spend")
            min_daily_spend = _as_decimal(raw)
        out.append(
            ChannelMetric(
                channel_id=channel_id,
                name=name,
                spend=bucket["spend"],
                clicks=bucket["clicks"],
                impressions=bucket["impressions"],
                conversions=bucket["conversions"],
                min_daily_spend=min_daily_spend,
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
