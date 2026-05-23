"""Analytics & Optimisation agent (W37, E10-S02/E10-S03).

Thin shim. The actual detection + recommendation logic lives in
`app.analytics.anomaly` and `app.analytics.recommendations`; this module
wires them to the agent_log / agent table so observability matches the
other agents.

`analyse_campaign` is the one nightly-grade entry point: it runs both
the anomaly detector and the recommendation generator, then optionally
auto-pauses when the tenant has opted in and the auto-pause heuristic
triggers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.anomaly import (
    detect_anomalies,
    should_auto_pause,
)
from app.analytics.recommendations import generate_recommendations
from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import AgentKind, CampaignStatus
from app.db.models import (
    Agent,
    Campaign,
    MetricAnomaly,
    OptimisationRecommendation,
)


async def ensure_analytics_agent(session: AsyncSession, tenant_id: UUID) -> Agent:
    """Get-or-create the per-tenant Analytics & Optimisation agent row."""
    existing = (
        await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == AgentKind.analytics_optimisation,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    agent = Agent(
        tenant_id=tenant_id,
        name="Analytics & Optimisation",
        agent_type=AgentKind.analytics_optimisation,
    )
    session.add(agent)
    await session.flush()
    return agent


@dataclass(frozen=True)
class AnalysisResult:
    """One nightly pass over a campaign."""

    campaign_id: UUID
    anomalies: list[MetricAnomaly]
    recommendations: list[OptimisationRecommendation]
    auto_paused: bool = False


async def analyse_campaign(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    campaign_id: UUID,
    now: datetime,
) -> AnalysisResult:
    """Run the W37 nightly pass: detect anomalies, generate
    recommendations, optionally auto-pause if the tenant opted in and
    the auto-pause heuristic fires.
    """
    anomalies = await detect_anomalies(
        session, tenant_id=tenant_id, campaign_id=campaign_id, now=now
    )
    recommendations = await generate_recommendations(
        session, tenant_id=tenant_id, campaign_id=campaign_id, now=now
    )

    auto_paused = False
    if anomalies and await should_auto_pause(
        session, tenant_id=tenant_id, campaign_id=campaign_id
    ):
        campaign = await session.get(Campaign, campaign_id)
        if campaign is not None and campaign.status != CampaignStatus.paused:
            prior_status = campaign.status
            campaign.status = CampaignStatus.paused
            write_audit(
                session,
                tenant_id=tenant_id,
                actor_kind=current_actor_kind.get(),
                actor_id=current_actor_id.get(),
                entity_kind="campaign",
                entity_id=campaign_id,
                action="campaign_auto_paused",
                before_state={"status": prior_status.value},
                after_state={"status": CampaignStatus.paused.value},
                metadata={
                    "trigger": "analytics_auto_pause",
                    "anomaly_ids": [str(a.id) for a in anomalies],
                },
            )
            auto_paused = True

    return AnalysisResult(
        campaign_id=campaign_id,
        anomalies=anomalies,
        recommendations=recommendations,
        auto_paused=auto_paused,
    )


__all__ = ["ensure_analytics_agent", "analyse_campaign", "AnalysisResult"]
