"""Campaign Strategist agent (W20, E05-S01/02/05).

Loads campaign + audience + tenant constraints + prior-version overrides,
drives the LLM planner, and persists the resulting proposal as a new
`strategy_proposal` row. Returns the row id + version for the queue handler
to stamp into `task.output_data`.

The planner is the noisy part (LLM call, JSON validation, retries). This
module is just the DB-bound wiring around it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._strategist_planner import (
    ChannelInfo,
    HumanOverride,
    StrategistPlanner,
    StrategyContext,
)
from app.db.enums import AgentKind, TenantConstraintKind
from app.db.models import (
    Agent,
    Audience,
    Campaign,
    Channel,
    StrategyProposal,
    TenantConstraint,
)


class StrategistPreconditionError(Exception):
    """Raised when the campaign isn't ready for a strategy proposal — surfaced
    at submit time, before the worker is involved (AC E05-S01 #4)."""


async def ensure_strategist_agent(session: AsyncSession, tenant_id: UUID) -> Agent:
    existing = (
        await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == AgentKind.campaign_strategist,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    agent = Agent(
        tenant_id=tenant_id,
        name="Campaign Strategist",
        agent_type=AgentKind.campaign_strategist,
    )
    session.add(agent)
    await session.flush()
    return agent


async def assert_preconditions(session: AsyncSession, campaign: Campaign) -> None:
    """E05-S01 AC #4: precondition check that runs at API submit time."""
    if not campaign.objective or not campaign.objective.strip():
        raise StrategistPreconditionError("campaign objective is empty")

    audience_exists = (
        await session.execute(
            select(func.count())
            .select_from(Audience)
            .where(Audience.campaign_id == campaign.id)
        )
    ).scalar_one()
    if audience_exists == 0:
        raise StrategistPreconditionError(
            "no audience materialised for this campaign — run Audience Targeting first"
        )

    channels_active = (
        await session.execute(
            select(func.count())
            .select_from(Channel)
            .where(Channel.tenant_id == campaign.tenant_id, Channel.is_active.is_(True))
        )
    ).scalar_one()
    if channels_active == 0:
        raise StrategistPreconditionError(
            "no active channels configured for this tenant"
        )


async def propose(
    session: AsyncSession,
    *,
    campaign_id: UUID,
    planner: StrategistPlanner,
    triggered_by_user_id: UUID | None,
) -> dict[str, Any]:
    """Build context, run the planner, persist a new proposal version.

    Returns a serialisable summary (proposal_id, version, warnings_count) that
    the queue handler writes into `task.output_data`.
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise StrategistPreconditionError(f"campaign {campaign_id} not found")

    await assert_preconditions(session, campaign)

    ctx = await _build_context(session, campaign)
    result = await planner.propose(ctx)

    next_version = (
        await session.execute(
            select(func.coalesce(func.max(StrategyProposal.version), 0) + 1).where(
                StrategyProposal.campaign_id == campaign.id
            )
        )
    ).scalar_one()

    actor_kind: str
    actor_id: UUID | None
    if triggered_by_user_id is not None:
        actor_kind = "user"
        actor_id = triggered_by_user_id
    else:
        actor_kind = "agent"
        agent = await ensure_strategist_agent(session, campaign.tenant_id)
        actor_id = agent.id

    proposal = StrategyProposal(
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        version=next_version,
        payload=result.payload,
        is_accepted=False,
        created_by_kind=actor_kind,
        created_by_id=actor_id,
        validation_warnings=result.validation_warnings,
    )
    session.add(proposal)
    await session.flush()

    return {
        "proposal_id": str(proposal.id),
        "campaign_id": str(campaign.id),
        "version": next_version,
        "warnings_count": len(result.validation_warnings),
        "attempts": result.attempts,
    }


async def _build_context(session: AsyncSession, campaign: Campaign) -> StrategyContext:
    audience_row = (
        await session.execute(
            select(Audience)
            .where(Audience.campaign_id == campaign.id)
            .order_by(Audience.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    audience_size = int(audience_row.actual_size or audience_row.estimated_size or 0) if audience_row else 0
    audience_summary = _summarise_audience(audience_row)

    channels = (
        await session.execute(
            select(Channel)
            .where(Channel.tenant_id == campaign.tenant_id, Channel.is_active.is_(True))
            .order_by(Channel.name.asc())
        )
    ).scalars().all()

    constraints = (
        await session.execute(
            select(TenantConstraint).where(TenantConstraint.tenant_id == campaign.tenant_id)
        )
    ).scalars().all()
    forbidden = [
        str(c.payload.get("platform", "")).lower()
        for c in constraints
        if c.kind == TenantConstraintKind.forbid_channel.value
        and c.payload.get("platform")
    ]
    hard_caps = [
        dict(c.payload) for c in constraints if c.kind == TenantConstraintKind.hard_cap.value
    ]

    overrides = await _carry_overrides(session, campaign_id=campaign.id)

    return StrategyContext(
        campaign_name=campaign.name,
        campaign_type=campaign.campaign_type.value,
        objective=campaign.objective,
        brief=campaign.brief,
        budget_total=Decimal(campaign.budget_total),
        currency=campaign.currency,
        start_date=campaign.start_date.isoformat(),
        end_date=campaign.end_date.isoformat(),
        audience_size=audience_size,
        audience_summary=audience_summary,
        available_channels=[ChannelInfo(platform=c.platform.value, name=c.name) for c in channels],
        forbidden_platforms=forbidden,
        hard_caps=hard_caps,
        human_overrides=overrides,
    )


def _summarise_audience(audience: Audience | None) -> str:
    if audience is None:
        return "no audience materialised"
    return f"audience '{audience.name}' (criteria: {audience.segment_criteria})"


async def _carry_overrides(
    session: AsyncSession, *, campaign_id: UUID
) -> list[HumanOverride]:
    """Pull the most recent version's `human_override=true` channel rows so a
    re-plan treats them as hard constraints (E05-S02 AC #2)."""
    latest = (
        await session.execute(
            select(StrategyProposal)
            .where(StrategyProposal.campaign_id == campaign_id)
            .order_by(StrategyProposal.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return []

    out: list[HumanOverride] = []
    for ch in latest.payload.get("channels", []):
        if not isinstance(ch, dict) or not ch.get("human_override"):
            continue
        try:
            out.append(
                HumanOverride(
                    platform=str(ch["platform"]),
                    allocation_pct=Decimal(str(ch["allocation_pct"])),
                    allocation_amount=Decimal(str(ch["allocation_amount"])),
                )
            )
        except (KeyError, ArithmeticError, ValueError):
            continue
    return out
