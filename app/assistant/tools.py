"""Tools the dashboard assistant can call (W42).

Each tool is a function the LLM picks via Anthropic tool-use. The
function:
  1. Validates inputs (FastAPI did this at HTTP boundary; we do it
     again here because the LLM might fabricate a UUID).
  2. Calls the same business logic the REST endpoints use — no
     duplication.
  3. Returns a `ToolResult` with a short user-facing message and an
     optional structured payload the template can render.

Tool definitions (the JSON schemas Claude sees) are at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import (
    AssetStatus,
    CampaignStatus,
    CampaignType,
    UserRole,
)
from app.db.models import (
    AppUser,
    Campaign,
    ContentAsset,
    MetricAnomaly,
    OptimisationRecommendation,
)


# ---------------------------------------------------------------------------
# Common return shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    """What a tool returns to the assistant router."""

    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False  # destructive ops set this True


class ToolError(Exception):
    """Raised when a tool can't run — bad input, missing object, wrong role."""


class ToolPermissionError(ToolError):
    """User role doesn't satisfy the tool's required role."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _find_campaign(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    identifier: str,
) -> Campaign:
    """Look up a campaign by UUID or by exact/contains name match."""
    try:
        cid = UUID(identifier)
        c = await session.get(Campaign, cid)
        if c is not None and c.tenant_id == tenant_id:
            return c
    except (ValueError, TypeError):
        pass
    # Name-based lookup. Case-insensitive contains.
    rows = (
        await session.execute(
            select(Campaign).where(
                Campaign.tenant_id == tenant_id,
                Campaign.name.ilike(f"%{identifier}%"),
            )
        )
    ).scalars().all()
    if not rows:
        raise ToolError(f"No campaign matches '{identifier}'.")
    if len(rows) > 1:
        names = ", ".join(f"'{r.name}'" for r in rows[:5])
        raise ToolError(
            f"'{identifier}' matches multiple campaigns ({names}). "
            "Be more specific."
        )
    return rows[0]


def _require_role(user: AppUser, minimum: UserRole) -> None:
    """Mirror of `app.api.deps.require_role` for the tool layer."""
    order = {
        UserRole.viewer: 0,
        UserRole.marketer: 1,
        UserRole.manager: 2,
        UserRole.admin: 3,
    }
    if order[user.role] < order[minimum]:
        raise ToolPermissionError(
            f"Your role '{user.role.value}' isn't allowed to do this — "
            f"need '{minimum.value}' or higher."
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def list_campaigns(
    session: AsyncSession,
    *,
    user: AppUser,
    status: str | None = None,
) -> ToolResult:
    """Tool: list campaigns the user can see, optionally filtered by status."""
    _require_role(user, UserRole.viewer)
    stmt = select(Campaign).where(Campaign.tenant_id == user.tenant_id)
    if status:
        try:
            CampaignStatus(status)
        except ValueError as exc:
            raise ToolError(f"Unknown status '{status}'.") from exc
        stmt = stmt.where(Campaign.status == status)
    stmt = stmt.order_by(Campaign.updated_at.desc()).limit(20)
    rows = (await session.execute(stmt)).scalars().all()
    items = [
        {
            "id": str(c.id),
            "name": c.name,
            "status": c.status.value,
            "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat(),
            "budget": f"{c.budget_total} {c.currency}",
        }
        for c in rows
    ]
    summary = (
        f"Found {len(items)} campaign{'s' if len(items) != 1 else ''}"
        + (f" in status '{status}'" if status else "")
        + "."
    )
    return ToolResult(summary=summary, data={"campaigns": items})


async def get_campaign(
    session: AsyncSession,
    *,
    user: AppUser,
    identifier: str,
) -> ToolResult:
    _require_role(user, UserRole.viewer)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=identifier)
    return ToolResult(
        summary=f"Campaign '{c.name}' is in status '{c.status.value}'.",
        data={
            "id": str(c.id),
            "name": c.name,
            "status": c.status.value,
            "type": c.campaign_type.value,
            "objective": c.objective,
            "brief_excerpt": (c.brief or "")[:300],
            "budget": f"{c.budget_total} {c.currency}",
            "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat(),
            "link": f"/ui/campaigns/{c.id}",
        },
    )


async def pause_campaign(
    session: AsyncSession,
    *,
    user: AppUser,
    identifier: str,
    confirm: bool = False,
) -> ToolResult:
    _require_role(user, UserRole.manager)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=identifier)
    if c.status == CampaignStatus.paused:
        return ToolResult(summary=f"Campaign '{c.name}' is already paused.")
    if not confirm:
        return ToolResult(
            summary=(
                f"Confirm: pause '{c.name}'? This cancels every queued dispatch "
                "task. Running tasks finish. Reply 'yes' or include "
                "`confirm: true` to proceed."
            ),
            data={"campaign_id": str(c.id), "campaign_name": c.name},
            requires_confirmation=True,
        )
    from app.agents.distribution import pause_campaign as _pause

    await _pause(session, campaign=c, reason="manual_via_assistant")
    return ToolResult(
        summary=f"Paused '{c.name}'. Queued dispatch tasks cancelled.",
        data={"campaign_id": str(c.id), "status": "paused"},
    )


async def resume_campaign(
    session: AsyncSession,
    *,
    user: AppUser,
    identifier: str,
) -> ToolResult:
    _require_role(user, UserRole.manager)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=identifier)
    if c.status != CampaignStatus.paused:
        raise ToolError(
            f"Campaign '{c.name}' is in '{c.status.value}' — only paused "
            "campaigns can be resumed."
        )
    from app.orchestrator.state_machine import campaign_sm

    await campaign_sm.apply(session, c, "resume")
    return ToolResult(
        summary=f"Resumed '{c.name}'. Dispatch tasks re-enqueued.",
        data={"campaign_id": str(c.id), "status": c.status.value},
    )


async def complete_campaign(
    session: AsyncSession,
    *,
    user: AppUser,
    identifier: str,
    confirm: bool = False,
) -> ToolResult:
    _require_role(user, UserRole.manager)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=identifier)
    if c.status == CampaignStatus.completed:
        return ToolResult(summary=f"'{c.name}' is already completed.")
    if not confirm:
        return ToolResult(
            summary=(
                f"Confirm: mark '{c.name}' completed? This auto-generates an "
                "end-of-campaign report and locks spend after reconciliation. "
                "Reply 'yes' to proceed."
            ),
            data={"campaign_id": str(c.id)},
            requires_confirmation=True,
        )
    from app.orchestrator.state_machine import campaign_sm

    await campaign_sm.apply(session, c, "complete_campaign")
    return ToolResult(
        summary=f"Marked '{c.name}' completed. Report generated.",
        data={
            "campaign_id": str(c.id),
            "status": c.status.value,
            "report_link": f"/ui/campaigns/{c.id}/report",
        },
    )


async def show_anomalies(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str | None = None,
    severity: str | None = None,
) -> ToolResult:
    _require_role(user, UserRole.viewer)
    stmt = select(MetricAnomaly).where(
        MetricAnomaly.tenant_id == user.tenant_id,
        MetricAnomaly.dismissed_at.is_(None),
    )
    if campaign:
        c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)
        stmt = stmt.where(MetricAnomaly.campaign_id == c.id)
    if severity:
        if severity not in {"warning", "critical"}:
            raise ToolError("severity must be 'warning' or 'critical'.")
        stmt = stmt.where(MetricAnomaly.severity == severity)
    stmt = stmt.order_by(MetricAnomaly.created_at.desc()).limit(20)
    rows = (await session.execute(stmt)).scalars().all()
    items = [
        {
            "id": str(a.id),
            "metric": a.metric,
            "severity": a.severity,
            "sigma": str(a.sigma),
            "observed": str(a.observed_value),
            "baseline_median": str(a.baseline_median),
            "campaign_id": str(a.campaign_id),
        }
        for a in rows
    ]
    return ToolResult(
        summary=f"{len(items)} open anomal{'y' if len(items) == 1 else 'ies'} found.",
        data={"anomalies": items},
    )


async def show_recommendations(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str | None = None,
) -> ToolResult:
    _require_role(user, UserRole.viewer)
    stmt = select(OptimisationRecommendation).where(
        OptimisationRecommendation.tenant_id == user.tenant_id,
        OptimisationRecommendation.status == "pending",
    )
    if campaign:
        c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)
        stmt = stmt.where(OptimisationRecommendation.campaign_id == c.id)
    stmt = stmt.order_by(OptimisationRecommendation.created_at.desc()).limit(10)
    rows = (await session.execute(stmt)).scalars().all()
    items = [
        {
            "id": str(r.id),
            "kind": r.kind,
            "rationale": r.rationale,
            "predicted_uplift": str(r.predicted_uplift) if r.predicted_uplift else None,
            "campaign_id": str(r.campaign_id),
        }
        for r in rows
    ]
    return ToolResult(
        summary=f"{len(items)} pending recommendation{'s' if len(items) != 1 else ''}.",
        data={"recommendations": items},
    )


async def show_pending_approvals(
    session: AsyncSession, *, user: AppUser
) -> ToolResult:
    _require_role(user, UserRole.viewer)
    rows = (
        await session.execute(
            select(ContentAsset).where(
                ContentAsset.tenant_id == user.tenant_id,
                ContentAsset.status == AssetStatus.pending_approval,
            ).order_by(ContentAsset.updated_at.desc()).limit(20)
        )
    ).scalars().all()
    items = [
        {
            "id": str(a.id),
            "title": a.title or "(no title)",
            "asset_type": a.asset_type.value,
            "campaign_id": str(a.campaign_id),
            "review_link": f"/ui/approvals/{a.id}",
        }
        for a in rows
    ]
    return ToolResult(
        summary=f"{len(items)} asset{'s' if len(items) != 1 else ''} waiting on approval.",
        data={"approvals": items, "queue_link": "/ui/approvals/queue"},
    )


async def summarise_kpis(
    session: AsyncSession,
    *,
    user: AppUser,
    identifier: str,
) -> ToolResult:
    _require_role(user, UserRole.viewer)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=identifier)
    from app.analytics.kpi_rollup import compute_campaign_kpis

    snap = await compute_campaign_kpis(
        session,
        tenant_id=user.tenant_id,
        campaign_id=c.id,
        now=datetime.now(UTC),
    )
    kpis = snap.kpis.as_dict()
    return ToolResult(
        summary=(
            f"'{c.name}': {kpis['opens']} opens, {kpis['clicks']} clicks, "
            f"{kpis['conversions']} conversions, ${kpis['spend']} spent."
        ),
        data={
            "kpis": kpis,
            "link": f"/ui/campaigns/{c.id}",
        },
    )


async def create_campaign(
    session: AsyncSession,
    *,
    user: AppUser,
    name: str,
    campaign_type: str,
    objective: str,
    brief: str,
    budget_total: str,
    currency: str = "USD",
    start_date: str,
    end_date: str,
    primary_kpi_metric: str = "conversion",
    primary_kpi_target: int = 100,
) -> ToolResult:
    """Tool: create a campaign in `drafted` status. Strategist + Content
    Creator runs are separate tools (so the user can review before
    paying for LLM calls)."""
    _require_role(user, UserRole.marketer)
    try:
        ctype = CampaignType(campaign_type)
    except ValueError as exc:
        valid = ", ".join(t.value for t in CampaignType)
        raise ToolError(
            f"Unknown campaign_type '{campaign_type}'. Valid: {valid}."
        ) from exc
    try:
        budget = Decimal(budget_total)
    except InvalidOperation as exc:
        raise ToolError(f"budget_total '{budget_total}' isn't a number.") from exc
    try:
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ToolError(
            "Dates must be ISO format (YYYY-MM-DD)."
        ) from exc
    if ed < sd:
        raise ToolError("end_date is before start_date.")

    c = Campaign(
        tenant_id=user.tenant_id,
        owner_id=user.id,
        name=name.strip(),
        campaign_type=ctype,
        objective=objective.strip(),
        brief=brief.strip(),
        budget_total=budget,
        currency=currency.upper(),
        start_date=sd,
        end_date=ed,
        kpi_targets={
            "primary": {
                "metric": primary_kpi_metric,
                "target": int(primary_kpi_target),
            },
            "secondary": [],
        },
        status=CampaignStatus.drafted,
    )
    session.add(c)
    await session.flush()
    return ToolResult(
        summary=(
            f"Created campaign '{c.name}' (status: drafted). Next step: "
            "upload audience CSV or build from CRM, then ask me to run the "
            "Strategist."
        ),
        data={
            "campaign_id": str(c.id),
            "link": f"/ui/campaigns/{c.id}",
        },
    )


async def accept_recommendation(
    session: AsyncSession,
    *,
    user: AppUser,
    identifier: str,
    confirm: bool = False,
) -> ToolResult:
    _require_role(user, UserRole.marketer)
    try:
        rid = UUID(identifier)
    except (ValueError, TypeError) as exc:
        raise ToolError("recommendation identifier must be a UUID.") from exc
    rec = await session.get(OptimisationRecommendation, rid)
    if rec is None or rec.tenant_id != user.tenant_id:
        raise ToolError(f"No recommendation found with id {rid}.")
    if rec.status != "pending":
        raise ToolError(f"Recommendation is in status '{rec.status}', not pending.")
    if not confirm:
        return ToolResult(
            summary=(
                f"Confirm: accept the {rec.kind} recommendation? "
                f"Predicted uplift: {rec.predicted_uplift or '—'}. "
                "This applies the change immediately."
            ),
            data={"recommendation_id": str(rec.id), "kind": rec.kind},
            requires_confirmation=True,
        )

    # Reuse the API helper so the apply logic isn't duplicated.
    from app.api.analytics import _apply_budget_shift

    if rec.kind == "budget_shift":
        await _apply_budget_shift(session, rec=rec)
    rec.status = "applied"
    rec.applied_at = datetime.now(UTC)
    rec.applied_by = user.id
    return ToolResult(
        summary=f"Applied {rec.kind} recommendation.",
        data={"recommendation_id": str(rec.id), "status": "applied"},
    )


async def dismiss_anomaly(
    session: AsyncSession,
    *,
    user: AppUser,
    identifier: str,
) -> ToolResult:
    _require_role(user, UserRole.admin)
    try:
        aid = UUID(identifier)
    except (ValueError, TypeError) as exc:
        raise ToolError("anomaly identifier must be a UUID.") from exc
    from app.analytics.anomaly import dismiss_anomaly as _dismiss

    try:
        row = await _dismiss(
            session, anomaly_id=aid, dismissed_by=user.id, now=datetime.now(UTC)
        )
    except LookupError as exc:
        raise ToolError(str(exc)) from exc
    return ToolResult(
        summary=f"Dismissed anomaly on metric '{row.metric}' (silenced 24h).",
        data={"anomaly_id": str(row.id)},
    )


# ---------------------------------------------------------------------------
# W42.2 — Interactive input + agent orchestration tools
# ---------------------------------------------------------------------------


async def request_input(
    session: AsyncSession,
    *,
    user: AppUser,
    prompt: str,
    options: list[str] | list[dict[str, str]] | None = None,
    context: str | None = None,
) -> ToolResult:
    """Surface a clickable question to the user.

    The model calls this when it needs a multiple-choice answer
    (campaign type, primary KPI metric, channel platform, yes/no
    confirmations beyond the destructive-tool path, etc.). The dashboard
    template renders the prompt + a chip per option; clicking a chip
    submits that value as the next user message."""
    _require_role(user, UserRole.viewer)
    raw_options = options or []
    normalised: list[dict[str, str]] = []
    for o in raw_options:
        if isinstance(o, str):
            normalised.append({"value": o, "label": o})
        elif isinstance(o, dict) and o.get("value"):
            normalised.append(
                {
                    "value": str(o["value"]),
                    "label": str(o.get("label") or o["value"]),
                }
            )
    return ToolResult(
        summary=prompt,
        data={"choices": normalised, "context": context},
        # Short-circuit the router so the model doesn't try to call
        # more tools while waiting for the user to click a chip.
        requires_confirmation=True,
    )


async def synthesise_audience(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str,
    size: int = 20,
    persona: str = "Demo contacts",
) -> ToolResult:
    """Create a synthetic Audience + members for demo/testing.

    For real production audiences, the user would upload a CSV or
    materialise from HubSpot. This tool exists so the assistant can
    drive an end-to-end demo flow without that setup."""
    _require_role(user, UserRole.marketer)
    if size < 1 or size > 200:
        raise ToolError("size must be between 1 and 200.")

    from app.db.models import Audience, AudienceMember

    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)
    aud = Audience(
        tenant_id=user.tenant_id,
        campaign_id=c.id,
        name=f"Demo audience — {persona[:40]}",
        segment_criteria={"_demo": True, "persona": persona},
        estimated_size=size,
        actual_size=size,
        refreshed_at=datetime.now(UTC),
    )
    session.add(aud)
    await session.flush()
    for i in range(size):
        session.add(
            AudienceMember(
                audience_id=aud.id,
                external_id=f"demo-{i:03d}",
                payload={
                    "email": f"contact{i:03d}@demo-co-{i % 5}.test",
                    "first_name": f"Demo{i}",
                    "company": f"Demo Co {i % 5}",
                },
                source="seed",
                fetched_at=datetime.now(UTC),
            )
        )
    # Advance the campaign state machine if appropriate.
    from app.db.enums import CampaignStatus

    if c.status == CampaignStatus.drafted:
        c.status = CampaignStatus.audience_built
    return ToolResult(
        summary=(
            f"Built a synthetic audience of {size} contacts for "
            f"'{c.name}'. The campaign is now in `audience_built` — "
            f"ready for the Strategist."
        ),
        data={
            "campaign_id": str(c.id),
            "audience_id": str(aud.id),
            "size": size,
        },
    )


async def generate_strategy(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str,
    confirm: bool = False,
) -> ToolResult:
    """Run the live Strategist agent against the campaign.

    Costs ~$0.02 in real Anthropic spend; we require a confirmation on
    the first call so the user explicitly opts in."""
    _require_role(user, UserRole.marketer)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)
    if not confirm:
        return ToolResult(
            summary=(
                f"This will make a real Anthropic call (~$0.02) to draft "
                f"a strategy for '{c.name}'. Continue?"
            ),
            data={"campaign_id": str(c.id), "campaign_name": c.name},
            requires_confirmation=True,
        )

    # Build the planner + run.
    from anthropic import AsyncAnthropic
    from app.agents.strategist import (
        StrategistPreconditionError,
        ensure_strategist_agent,
        propose as strategist_propose,
    )
    from app.agents._strategist_planner import StrategistPlanner
    from app.settings.config import get_settings
    from app.db.models import StrategyProposal

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ToolError(
            "ANTHROPIC_API_KEY is not configured — the Strategist needs a "
            "live LLM."
        )
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    planner = StrategistPlanner(client=client, model=settings.strategist_model)

    await ensure_strategist_agent(session, user.tenant_id)
    try:
        await strategist_propose(
            session,
            campaign_id=c.id,
            planner=planner,
            triggered_by_user_id=user.id,
        )
    except StrategistPreconditionError as exc:
        raise ToolError(str(exc)) from exc

    # Read back the new proposal so we can summarise.
    proposal = (
        await session.execute(
            select(StrategyProposal)
            .where(StrategyProposal.campaign_id == c.id)
            .order_by(StrategyProposal.version.desc())
            .limit(1)
        )
    ).scalar_one()

    channels = proposal.payload.get("channels", []) if proposal.payload else []
    channel_summary = ", ".join(
        f"{ch.get('name')} {ch.get('allocation_pct')}%" for ch in channels
    ) or "(no channels)"
    return ToolResult(
        summary=(
            f"Drafted proposal v{proposal.version}: {channel_summary}. "
            "Review it and accept when ready."
        ),
        data={
            "campaign_id": str(c.id),
            "proposal_id": str(proposal.id),
            "version": proposal.version,
            "proposal_payload": proposal.payload,
        },
    )


async def accept_strategy(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str,
    confirm: bool = False,
) -> ToolResult:
    """Accept the latest unaccepted proposal + seed the calendar."""
    _require_role(user, UserRole.marketer)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)
    from app.db.models import StrategyProposal

    proposal = (
        await session.execute(
            select(StrategyProposal)
            .where(
                StrategyProposal.campaign_id == c.id,
                StrategyProposal.is_accepted.is_(False),
            )
            .order_by(StrategyProposal.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise ToolError(
            f"No unaccepted proposal for '{c.name}'. Did you run the "
            "Strategist yet?"
        )
    if not confirm:
        return ToolResult(
            summary=(
                f"Accept proposal v{proposal.version} for '{c.name}'? "
                "This locks the channel mix + calendar."
            ),
            data={"proposal_id": str(proposal.id)},
            requires_confirmation=True,
        )

    proposal.is_accepted = True
    # Demote any previously-accepted proposal for the same campaign.
    await session.execute(
        StrategyProposal.__table__.update()
        .where(
            StrategyProposal.campaign_id == c.id,
            StrategyProposal.id != proposal.id,
            StrategyProposal.is_accepted.is_(True),
        )
        .values(is_accepted=False)
    )

    from app.agents.strategist import seed_calendar

    try:
        touchpoints = await seed_calendar(session, proposal=proposal)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Calendar seeding failed: {exc}") from exc

    from app.db.enums import CampaignStatus

    if c.status in (
        CampaignStatus.audience_built,
        CampaignStatus.drafted,
    ):
        c.status = CampaignStatus.strategy_set
    return ToolResult(
        summary=(
            f"Accepted proposal v{proposal.version}; {len(touchpoints)} "
            "touchpoint(s) scheduled. Ready to draft content."
        ),
        data={
            "campaign_id": str(c.id),
            "proposal_id": str(proposal.id),
            "touchpoints": len(touchpoints),
        },
    )


async def generate_content(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str,
    confirm: bool = False,
) -> ToolResult:
    """Seed asset rows per touchpoint and run the live Content Creator
    on each. Costs ~$0.02 × N assets in real Anthropic spend."""
    _require_role(user, UserRole.marketer)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)

    from app.agents.content_creator import (
        ContentCreatorError,
        ensure_content_creator_agent,
        generate_asset,
        seed_assets_for_campaign,
    )
    from app.db.enums import AssetStatus, CampaignStatus
    from app.db.models import ContentAsset
    from anthropic import AsyncAnthropic
    from app.settings.config import get_settings
    from app.tools.copywriting import CopywritingTool
    from app.tools.seo import SeoAnalysisTool

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ToolError(
            "ANTHROPIC_API_KEY is not configured — Content Creator needs "
            "a live LLM."
        )

    # First: figure out how many assets we'd generate, so the confirm
    # cost estimate is honest.
    from app.db.models import StrategyProposal, StrategyTouchpoint

    accepted = (
        await session.execute(
            select(StrategyProposal).where(
                StrategyProposal.campaign_id == c.id,
                StrategyProposal.is_accepted.is_(True),
            )
        )
    ).scalar_one_or_none()
    if accepted is None:
        raise ToolError(
            "No accepted strategy proposal yet. Run + accept the Strategist first."
        )

    touchpoints = (
        await session.execute(
            select(StrategyTouchpoint).where(
                StrategyTouchpoint.proposal_id == accepted.id
            )
        )
    ).scalars().all()
    if not touchpoints:
        raise ToolError("The accepted proposal has no touchpoints to draft for.")
    n_assets = len(touchpoints)

    if not confirm:
        return ToolResult(
            summary=(
                f"This will draft {n_assets} content asset(s) via real "
                f"Anthropic calls (~${0.015 * n_assets:.3f}). Continue?"
            ),
            data={"campaign_id": str(c.id), "n_assets": n_assets},
            requires_confirmation=True,
        )

    # Seed asset rows (or reuse existing drafts that haven't been generated).
    await ensure_content_creator_agent(session, user.tenant_id)
    try:
        asset_rows = await seed_assets_for_campaign(session, campaign=c)
    except ContentCreatorError as exc:
        raise ToolError(str(exc)) from exc

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    copy_tool = CopywritingTool(client=client, model=settings.copywriting_model)
    seo_tool = SeoAnalysisTool()

    succeeded = 0
    failed: list[dict[str, str]] = []
    for asset in asset_rows:
        try:
            await generate_asset(
                session,
                asset_id=asset.id,
                copywriting_tool=copy_tool,
                seo_tool=seo_tool,
            )
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            failed.append({"asset_id": str(asset.id), "error": str(exc)[:200]})

    # State advance: drafted assets → content_in_production was already
    # set by seed_assets_for_campaign indirectly via the state machine
    # for the campaign. Here we just ensure the campaign tracks it.
    if c.status == CampaignStatus.strategy_set:
        c.status = CampaignStatus.content_in_production
    return ToolResult(
        summary=(
            f"Drafted {succeeded}/{n_assets} asset(s)"
            + (f"; {len(failed)} failed" if failed else "")
            + ". Review them and approve when ready."
        ),
        data={
            "campaign_id": str(c.id),
            "succeeded": succeeded,
            "failed": failed,
        },
    )


async def approve_all_content(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str,
    confirm: bool = False,
) -> ToolResult:
    """Bulk-approve every drafted / pending_approval asset on the
    campaign. Manager+ only — the human approval gate is intentionally
    not removable from the system, but this tool moves it to a single
    decision in the assistant flow for demo speed."""
    _require_role(user, UserRole.manager)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)

    from app.db.enums import AssetStatus, CampaignStatus
    from app.db.models import ContentAsset

    rows = (
        await session.execute(
            select(ContentAsset).where(
                ContentAsset.campaign_id == c.id,
                ContentAsset.status.in_(
                    [AssetStatus.drafted, AssetStatus.pending_approval]
                ),
            )
        )
    ).scalars().all()
    if not rows:
        return ToolResult(
            summary=f"No drafts to approve on '{c.name}'.",
            data={"campaign_id": str(c.id), "approved": 0},
        )

    if not confirm:
        return ToolResult(
            summary=(
                f"Approve all {len(rows)} draft asset(s) on '{c.name}' in "
                "one click? Each approval will land in the audit log."
            ),
            data={"campaign_id": str(c.id), "count": len(rows)},
            requires_confirmation=True,
        )

    for row in rows:
        row.status = AssetStatus.approved
    await session.flush()

    advanced_to = await _try_advance_after_approval(session, c)
    if advanced_to == "ready_to_launch":
        tail = "Campaign is now in `ready_to_launch` — ready to go live."
    else:
        tail = f"Campaign is in `{c.status.value}`."
    return ToolResult(
        summary=f"Approved {len(rows)} asset(s). {tail}",
        data={
            "campaign_id": str(c.id),
            "approved": len(rows),
            "campaign_status": c.status.value,
        },
    )


async def list_drafts_for_review(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str | None = None,
) -> ToolResult:
    """List drafted / pending_approval content assets for a campaign so
    the user can review and approve each one individually.

    If `campaign` is omitted, falls back to the assistant's active
    campaign pointer (W42.3) — covers "show me the drafts" without the
    user re-naming the campaign."""
    _require_role(user, UserRole.viewer)
    from app.assistant.memory import get_active_campaign

    resolved_id: UUID | None
    resolved_name: str
    if campaign:
        c = await _find_campaign(
            session, tenant_id=user.tenant_id, identifier=campaign
        )
        resolved_id = c.id
        resolved_name = c.name
    else:
        active = await get_active_campaign(session, user_id=user.id)
        if active is None:
            raise ToolError(
                "No active campaign in this conversation. Tell me which "
                "campaign to review, or say 'where did we leave off' to "
                "see the last one I touched."
            )
        c = await session.get(Campaign, active)
        if c is None or c.tenant_id != user.tenant_id:
            raise ToolError("Active campaign no longer exists.")
        resolved_id = c.id
        resolved_name = c.name

    from app.db.models import ContentAsset

    rows = (
        await session.execute(
            select(ContentAsset)
            .where(
                ContentAsset.campaign_id == resolved_id,
                ContentAsset.status.in_(
                    [AssetStatus.drafted, AssetStatus.pending_approval]
                ),
            )
            .order_by(
                ContentAsset.scheduled_at.asc().nullslast(),
                ContentAsset.created_at.asc(),
            )
        )
    ).scalars().all()

    items: list[dict[str, Any]] = []
    for a in rows:
        fields = (a.extra_metadata or {}).get("fields") or {}
        body_preview = (a.content or "").strip().split("\n")[0][:160]
        flags = (a.extra_metadata or {}).get("compliance_violations") or []
        items.append(
            {
                "id": str(a.id),
                "title": fields.get("subject") or a.title or "(no title)",
                "asset_type": a.asset_type.value,
                "status": a.status.value,
                "preview": body_preview,
                "compliance_flags": flags,
                "scheduled_at": a.scheduled_at.isoformat() if a.scheduled_at else None,
            }
        )

    return ToolResult(
        summary=(
            f"{len(items)} draft{'s' if len(items) != 1 else ''} on "
            f"'{resolved_name}' waiting on review."
        ),
        data={
            "drafts": items,
            "campaign_id": str(resolved_id),
            "campaign_name": resolved_name,
        },
    )


async def approve_asset(
    session: AsyncSession,
    *,
    user: AppUser,
    asset_id: str,
) -> ToolResult:
    """Approve one drafted / pending_approval content asset. Manager+
    only. Mirrors the W25 API endpoint logic (compliance gate, audit)."""
    _require_role(user, UserRole.manager)
    try:
        aid = UUID(asset_id)
    except (ValueError, TypeError) as exc:
        raise ToolError("asset_id must be a UUID.") from exc

    from app.db.models import ContentAsset

    asset = await session.get(ContentAsset, aid)
    if asset is None or asset.tenant_id != user.tenant_id:
        raise ToolError(f"No content asset with id {aid}.")
    if asset.status not in {
        AssetStatus.drafted,
        AssetStatus.pending_approval,
        AssetStatus.rejected,
    }:
        raise ToolError(
            f"Asset is in '{asset.status.value}' — only drafted / "
            "pending_approval / rejected assets can be approved."
        )
    flags = (asset.extra_metadata or {}).get("compliance_violations") or []
    if any(
        (isinstance(f, dict) and f.get("severity") == "blocker") for f in flags
    ):
        raise ToolError(
            "Asset is compliance-blocked — clear compliance via the "
            "asset's review page in the UI first."
        )

    asset.status = AssetStatus.approved
    await session.flush()

    campaign = await session.get(Campaign, asset.campaign_id)
    advanced_to: str | None = None
    if campaign is not None:
        advanced_to = await _try_advance_after_approval(session, campaign)

    summary = f"Approved '{asset.title or asset.id}'."
    if advanced_to == "ready_to_launch":
        summary += " Every required asset is approved — campaign is now ready to launch."
    return ToolResult(
        summary=summary,
        data={
            "asset_id": str(asset.id),
            "campaign_id": str(asset.campaign_id),
            "status": "approved",
            "campaign_status": (
                campaign.status.value if campaign is not None else None
            ),
        },
    )


async def _try_advance_after_approval(
    session: AsyncSession, campaign: Campaign
) -> str | None:
    """Mirror the API-side `_maybe_advance_to_ready_to_launch`, but also
    handle the case where the campaign hasn't been `submit_for_approval`-ed
    yet (which is normal when the assistant drives per-asset approvals
    straight off the drafted set)."""
    from app.orchestrator.state_machine import (
        GuardFailedError,
        UnknownTransitionError,
        campaign_sm,
    )

    if campaign.status == CampaignStatus.content_in_production:
        try:
            await campaign_sm.apply(session, campaign, "submit_for_approval")
        except (UnknownTransitionError, GuardFailedError):
            return None
    if campaign.status == CampaignStatus.approval_pending:
        try:
            await campaign_sm.apply(session, campaign, "start_launch")
        except (UnknownTransitionError, GuardFailedError):
            return None
    return campaign.status.value if campaign.status == CampaignStatus.ready_to_launch else None


async def reject_asset(
    session: AsyncSession,
    *,
    user: AppUser,
    asset_id: str,
    reason: str | None = None,
) -> ToolResult:
    """Reject one drafted / pending_approval content asset. Manager+
    only. The note is stored on the asset's metadata so the Content
    Creator can read it on regeneration."""
    _require_role(user, UserRole.manager)
    try:
        aid = UUID(asset_id)
    except (ValueError, TypeError) as exc:
        raise ToolError("asset_id must be a UUID.") from exc

    from app.db.models import ContentAsset

    asset = await session.get(ContentAsset, aid)
    if asset is None or asset.tenant_id != user.tenant_id:
        raise ToolError(f"No content asset with id {aid}.")
    if asset.status not in {AssetStatus.drafted, AssetStatus.pending_approval}:
        raise ToolError(
            f"Asset is in '{asset.status.value}' — only drafted / "
            "pending_approval assets can be rejected."
        )

    asset.status = AssetStatus.rejected
    # Assistant-driven rejection means "drop this variant from the campaign",
    # not "regenerate it" (the API flow queues a regenerate task). Flip
    # is_required so the rejection doesn't block the launch guard. The
    # audit trail still shows the rejection + reason.
    asset.is_required = False
    meta = dict(asset.extra_metadata or {})
    if reason:
        meta["last_rejection_reason"] = reason
    asset.extra_metadata = meta
    await session.flush()

    campaign = await session.get(Campaign, asset.campaign_id)
    if campaign is not None:
        await _try_advance_after_approval(session, campaign)

    return ToolResult(
        summary=(
            f"Rejected '{asset.title or asset.id}'. "
            f"{reason or '(no reason given)'}"
        ),
        data={
            "asset_id": str(asset.id),
            "campaign_id": str(asset.campaign_id),
            "status": "rejected",
            "reason": reason,
            "campaign_status": (
                campaign.status.value if campaign is not None else None
            ),
        },
    )


async def where_did_we_leave_off(
    session: AsyncSession, *, user: AppUser
) -> ToolResult:
    """Tell the user which campaign they were last working on, what
    state it's in, and what the suggested next step is. Powered by the
    persistent `active_campaign_id` pointer so this works after the
    message window trims earlier turns."""
    _require_role(user, UserRole.viewer)
    from app.assistant.memory import get_active_campaign, set_active_campaign

    active = await get_active_campaign(session, user_id=user.id)
    if active is None:
        return ToolResult(
            summary=(
                "No active campaign yet. Tell me what you'd like to "
                "create or which campaign to look at, and I'll pick up "
                "from there."
            ),
            data={
                "choices": [
                    {"value": "List my campaigns", "label": "List my campaigns"},
                    {"value": "Show me anything that needs my attention", "label": "Anything need attention?"},
                    {"value": "I want to create a new campaign", "label": "Start a new campaign"},
                ]
            },
            requires_confirmation=True,
        )
    c = await session.get(Campaign, active)
    if c is None or c.tenant_id != user.tenant_id:
        await set_active_campaign(
            session,
            user_id=user.id,
            tenant_id=user.tenant_id,
            campaign_id=None,
        )
        return ToolResult(
            summary=(
                "The campaign we were on is gone (deleted or out of "
                "reach). Pick something else?"
            ),
        )

    status = c.status.value
    next_step = {
        "drafted": "Build a synthetic audience",
        "audience_built": "Draft the strategy",
        "strategy_set": "Draft the content",
        "content_in_production": "Review the drafts",
        "approval_pending": "Review the drafts",
        "ready_to_launch": "Launch the campaign",
        "live": "Check current KPIs",
        "optimising": "Check current KPIs",
        "paused": "Resume or complete",
        "completed": "Open the end-of-campaign report",
    }.get(status, "Take a look")
    return ToolResult(
        summary=(
            f"You were working on '{c.name}'. It's in '{status}'. "
            f"Next: {next_step.lower()}."
        ),
        data={
            "campaign_id": str(c.id),
            "campaign_name": c.name,
            "status": status,
            "next_step": next_step,
            "link": f"/ui/campaigns/{c.id}",
        },
    )


async def launch_campaign(
    session: AsyncSession,
    *,
    user: AppUser,
    campaign: str,
    confirm: bool = False,
) -> ToolResult:
    """Move a `ready_to_launch` campaign to `live`. Manager+ only."""
    _require_role(user, UserRole.manager)
    c = await _find_campaign(session, tenant_id=user.tenant_id, identifier=campaign)
    from app.db.enums import CampaignStatus

    if c.status == CampaignStatus.live:
        return ToolResult(summary=f"'{c.name}' is already live.")
    if c.status != CampaignStatus.ready_to_launch:
        raise ToolError(
            f"'{c.name}' is in '{c.status.value}' — only "
            "`ready_to_launch` campaigns can be launched. Approve all "
            "assets first."
        )
    if not confirm:
        return ToolResult(
            summary=(
                f"Launch '{c.name}' now? Distribution starts immediately."
            ),
            data={"campaign_id": str(c.id)},
            requires_confirmation=True,
        )
    c.status = CampaignStatus.live
    c.launched_at = datetime.now(UTC)
    return ToolResult(
        summary=f"'{c.name}' is now LIVE. Watch the KPI dashboard for traffic.",
        data={
            "campaign_id": str(c.id),
            "status": "live",
            "link": f"/ui/campaigns/{c.id}",
        },
    )


# ---------------------------------------------------------------------------
# Tool catalog — JSON schemas Claude sees
# ---------------------------------------------------------------------------


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_campaigns",
        "description": "List campaigns visible to the current user, "
        "optionally filtered by status. Use when the user asks 'what's "
        "running', 'show me my campaigns', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [s.value for s in CampaignStatus],
                    "description": "Optional status filter.",
                },
            },
        },
    },
    {
        "name": "get_campaign",
        "description": "Get full detail on one campaign by UUID or name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "Campaign UUID or (partial) name.",
                },
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "create_campaign",
        "description": (
            "Create a new campaign in `drafted` status. Call this when the "
            "user provides a brief and wants to start a campaign. Required "
            "fields must be present; if anything is missing, ask the user "
            "for it instead of calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "campaign_type": {
                    "type": "string",
                    "enum": [t.value for t in CampaignType],
                },
                "objective": {"type": "string"},
                "brief": {"type": "string"},
                "budget_total": {"type": "string", "description": "Numeric amount, no currency symbol."},
                "currency": {"type": "string", "default": "USD"},
                "start_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                "primary_kpi_metric": {
                    "type": "string",
                    "enum": ["open", "click", "conversion", "reply", "impression"],
                },
                "primary_kpi_target": {"type": "integer"},
            },
            "required": [
                "name",
                "campaign_type",
                "objective",
                "brief",
                "budget_total",
                "start_date",
                "end_date",
            ],
        },
    },
    {
        "name": "pause_campaign",
        "description": (
            "Pause a campaign — cancels queued dispatch tasks. Manager+ "
            "only. Requires confirmation on first call: invoke with "
            "`confirm: false` first, then `confirm: true` after the user "
            "agrees."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "resume_campaign",
        "description": "Resume a paused campaign.",
        "input_schema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
    },
    {
        "name": "complete_campaign",
        "description": (
            "Mark a campaign completed. Triggers the end-of-campaign "
            "report. Requires confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "show_anomalies",
        "description": (
            "Show non-dismissed metric anomalies, optionally filtered by "
            "campaign + severity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {"type": "string"},
                "severity": {"type": "string", "enum": ["warning", "critical"]},
            },
        },
    },
    {
        "name": "show_recommendations",
        "description": "Show pending optimisation recommendations.",
        "input_schema": {
            "type": "object",
            "properties": {"campaign": {"type": "string"}},
        },
    },
    {
        "name": "show_pending_approvals",
        "description": "Show content assets waiting on human approval.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "summarise_kpis",
        "description": "Summarise the live KPI snapshot for one campaign.",
        "input_schema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
    },
    {
        "name": "accept_recommendation",
        "description": (
            "Accept (apply) one pending optimisation recommendation by "
            "UUID. Requires confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "dismiss_anomaly",
        "description": "Dismiss a metric anomaly (silences for 24h). Admin-only.",
        "input_schema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
    },
    {
        "name": "request_input",
        "description": (
            "ASK THE USER A STRUCTURED QUESTION. Prefer this over "
            "free-form text whenever the answer is multiple-choice (enum, "
            "yes/no, pick-one-of-N). The dashboard renders the prompt + a "
            "clickable chip per option. Clicking a chip resumes the "
            "conversation with that value as the user's next message. Use "
            "for: campaign_type, primary_kpi_metric, channel choices, "
            "audience source (synthetic vs CSV), next-step suggestions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The question shown to the user."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Plain string options. The chip label and submitted value are the same.",
                },
                "context": {"type": "string", "description": "Optional supporting context shown below the prompt."},
            },
            "required": ["prompt", "options"],
        },
    },
    {
        "name": "synthesise_audience",
        "description": (
            "Build a synthetic audience (N fake contacts) for a campaign "
            "in `drafted` status. Use for demo / testing when the user "
            "hasn't uploaded a CSV. After this, the campaign is in "
            "`audience_built` and ready for the Strategist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name or UUID."},
                "size": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                "persona": {"type": "string", "description": "Short description used in the audience name."},
            },
            "required": ["campaign"],
        },
    },
    {
        "name": "generate_strategy",
        "description": (
            "Run the LIVE Strategist agent against a campaign — makes a "
            "real Anthropic API call. Requires confirmation on first call. "
            "Returns the drafted proposal JSON; user reviews and then "
            "calls accept_strategy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["campaign"],
        },
    },
    {
        "name": "accept_strategy",
        "description": (
            "Accept the latest unaccepted Strategist proposal for a "
            "campaign. Seeds the touchpoint calendar. Requires confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["campaign"],
        },
    },
    {
        "name": "generate_content",
        "description": (
            "Draft content for every touchpoint via the LIVE Content "
            "Creator agent — makes one Anthropic call per touchpoint. "
            "Requires confirmation. Costs ~$0.015 per asset."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["campaign"],
        },
    },
    {
        "name": "approve_all_content",
        "description": (
            "Bulk-approve every drafted / pending_approval content asset "
            "on a campaign in one click. Manager+ only. Requires "
            "confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["campaign"],
        },
    },
    {
        "name": "launch_campaign",
        "description": (
            "Move a `ready_to_launch` campaign to `live`. Manager+ only. "
            "Requires confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["campaign"],
        },
    },
    {
        "name": "list_drafts_for_review",
        "description": (
            "List drafted / pending_approval content assets for a campaign "
            "so the user can review and approve each one individually. "
            "Call this when the user wants to 'review the drafts', 'go "
            "through the content one by one', or similar. If the user "
            "doesn't name a campaign, omit `campaign` — the assistant's "
            "active-campaign pointer will fill it in. Each draft renders "
            "with inline Approve / Reject chips."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {
                    "type": "string",
                    "description": "Optional campaign UUID or name. Omit to use the active campaign.",
                },
            },
        },
    },
    {
        "name": "approve_asset",
        "description": (
            "Approve one content asset (single, not bulk). Manager+ only. "
            "The asset_id is a UUID — look it up via list_drafts_for_review "
            "first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}},
            "required": ["asset_id"],
        },
    },
    {
        "name": "reject_asset",
        "description": (
            "Reject one content asset with an optional rejection reason. "
            "Manager+ only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["asset_id"],
        },
    },
    {
        "name": "where_did_we_leave_off",
        "description": (
            "Tell the user which campaign they were last working on, "
            "what state it's in, and what the natural next step is. "
            "Call this when the user says 'where were we', 'continue', "
            "'I'm back', or returns after a long pause. Powered by the "
            "persistent active_campaign_id pointer so it survives the "
            "message-window trim."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# Lookup table the router uses to dispatch a tool_use block to the right function.
TOOL_HANDLERS = {
    "list_campaigns": list_campaigns,
    "get_campaign": get_campaign,
    "create_campaign": create_campaign,
    "pause_campaign": pause_campaign,
    "resume_campaign": resume_campaign,
    "complete_campaign": complete_campaign,
    "show_anomalies": show_anomalies,
    "show_recommendations": show_recommendations,
    "show_pending_approvals": show_pending_approvals,
    "summarise_kpis": summarise_kpis,
    "accept_recommendation": accept_recommendation,
    "dismiss_anomaly": dismiss_anomaly,
    # W42.2 — interactive + orchestration
    "request_input": request_input,
    "synthesise_audience": synthesise_audience,
    "generate_strategy": generate_strategy,
    "accept_strategy": accept_strategy,
    "generate_content": generate_content,
    "approve_all_content": approve_all_content,
    "launch_campaign": launch_campaign,
    # W42.3 — approval workflow + persistent context
    "list_drafts_for_review": list_drafts_for_review,
    "approve_asset": approve_asset,
    "reject_asset": reject_asset,
    "where_did_we_leave_off": where_did_we_leave_off,
}


__all__ = [
    "ToolResult",
    "ToolError",
    "ToolPermissionError",
    "TOOL_DEFINITIONS",
    "TOOL_HANDLERS",
    "list_campaigns",
    "get_campaign",
    "create_campaign",
    "pause_campaign",
    "resume_campaign",
    "complete_campaign",
    "show_anomalies",
    "show_recommendations",
    "show_pending_approvals",
    "summarise_kpis",
    "accept_recommendation",
    "dismiss_anomaly",
    "request_input",
    "synthesise_audience",
    "generate_strategy",
    "accept_strategy",
    "generate_content",
    "approve_all_content",
    "launch_campaign",
    "list_drafts_for_review",
    "approve_asset",
    "reject_asset",
    "where_did_we_leave_off",
]
