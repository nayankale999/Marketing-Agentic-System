"""Content Creator agent (W22, E06-S01/04).

Consumes the accepted strategy + touchpoint calendar + brand voice and
produces `content_asset` rows. One task per touchpoint, dispatched by the
orchestrator when the `start_content` transition fires.

The agent's two responsibilities:

  * `seed_assets_for_campaign` — called once from the state machine when the
    campaign enters `content_in_production`. Pre-creates one content_asset
    row per touchpoint in `requested` state and enqueues one task per row.

  * `generate_asset` — called by the queue handler per task. Loads the row,
    flips it to `generating`, runs the copywriting tool with the brand voice
    + channel constraints, runs the brand-check + SEO tool where applicable,
    persists the result as `drafted`, and if it's the last required asset
    drives `submit_for_approval` so the campaign moves on to approval.

On unrecoverable failure the row is reverted to `requested` (so the
'regenerate' action lets the marketer rerun) and the exception propagates
to the queue handler's error path, which writes it into agent_log.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._content_planner import (
    AssetPlan,
    PlannerError,
    build_copywriting_inputs,
    bundle_metadata,
    extract_title,
    plan_for_platform,
)
from app.agents.brand_voice import check_dont_words, format_brand_voice_prompt
from app.db.enums import (
    AgentKind,
    AssetStatus,
    AssetType,
    CampaignStatus,
)
from app.db.models import (
    Agent,
    Audience,
    BrandVoice,
    Campaign,
    Channel,
    ContentAsset,
    StrategyProposal,
    StrategyTouchpoint,
)
from app.orchestrator.queue import enqueue_task
from app.tools import CopywritingTool, SeoAnalysisTool


class ContentCreatorError(Exception):
    """Raised on preconditions or unrecoverable generation failures."""


async def ensure_content_creator_agent(
    session: AsyncSession, tenant_id: UUID
) -> Agent:
    existing = (
        await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == AgentKind.content_creator,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    agent = Agent(
        tenant_id=tenant_id,
        name="Content Creator",
        agent_type=AgentKind.content_creator,
    )
    session.add(agent)
    await session.flush()
    return agent


async def seed_assets_for_campaign(
    session: AsyncSession, *, campaign: Campaign
) -> list[ContentAsset]:
    """Pre-create one content_asset per touchpoint and enqueue a generation
    task per row. Called from the `start_content` transition's on_enter.

    Returns the created rows so the caller can enumerate them. Raises
    `ContentCreatorError` if there's no accepted proposal or no touchpoints
    — callers map that to a 422 at the API layer."""
    accepted = (
        await session.execute(
            select(StrategyProposal).where(
                StrategyProposal.campaign_id == campaign.id,
                StrategyProposal.is_accepted.is_(True),
            )
        )
    ).scalar_one_or_none()
    if accepted is None:
        raise ContentCreatorError(
            "no accepted strategy proposal for this campaign"
        )

    touchpoints = (
        await session.execute(
            select(StrategyTouchpoint)
            .where(StrategyTouchpoint.proposal_id == accepted.id)
            .order_by(
                StrategyTouchpoint.scheduled_at.asc(),
                StrategyTouchpoint.position.asc(),
            )
        )
    ).scalars().all()
    if not touchpoints:
        raise ContentCreatorError("accepted proposal has no calendar touchpoints")

    agent = await ensure_content_creator_agent(session, campaign.tenant_id)

    # Resolve channel rows once (platform → Channel.id) so each asset can
    # carry a stable channel_id link for downstream Distribution.
    channels = (
        await session.execute(
            select(Channel).where(Channel.tenant_id == campaign.tenant_id)
        )
    ).scalars().all()
    channel_by_platform: dict[str, Channel] = {c.platform.value: c for c in channels}

    rows: list[ContentAsset] = []
    for tp in touchpoints:
        try:
            plan = plan_for_platform(tp.channel_platform)
        except PlannerError:
            # Skip unmappable platforms rather than blow up the whole seed —
            # the marketer can still launch the rest. Mark via audit elsewhere.
            continue

        channel = channel_by_platform.get(tp.channel_platform)
        asset = ContentAsset(
            tenant_id=campaign.tenant_id,
            campaign_id=campaign.id,
            channel_id=channel.id if channel else None,
            asset_type=plan.asset_type,
            status=AssetStatus.requested,
            is_required=True,
            scheduled_at=tp.scheduled_at,
            extra_metadata={
                "touchpoint_id": str(tp.id),
                "channel_platform": tp.channel_platform,
            },
        )
        session.add(asset)
        rows.append(asset)

    if not rows:
        raise ContentCreatorError(
            "no asset rows could be created — every touchpoint platform is unmapped"
        )

    await session.flush()

    for asset in rows:
        await enqueue_task(
            session,
            tenant_id=campaign.tenant_id,
            agent_id=agent.id,
            campaign_id=campaign.id,
            skill_name="content_creator.generate_asset",
            input_data={
                "asset_id": str(asset.id),
                "campaign_id": str(campaign.id),
            },
        )
    return rows


async def generate_asset(
    session: AsyncSession,
    *,
    asset_id: UUID,
    copywriting_tool: CopywritingTool,
    seo_tool: SeoAnalysisTool,
) -> dict[str, Any]:
    """Run one content_creator.generate_asset task. Returns a serialisable
    summary the queue handler writes into `task.output_data`. Raises on
    unrecoverable failures so the queue's retry/error path catches them."""
    asset = await session.get(ContentAsset, asset_id)
    if asset is None:
        raise ContentCreatorError(f"content_asset {asset_id} not found")

    campaign = await session.get(Campaign, asset.campaign_id)
    if campaign is None:
        raise ContentCreatorError(f"campaign {asset.campaign_id} not found")

    platform = str(asset.extra_metadata.get("channel_platform", "")).lower()
    try:
        plan = plan_for_platform(platform)
    except PlannerError as exc:
        # No retry will help; mark the row failed so the operator sees it.
        asset.status = AssetStatus.failed
        await session.flush()
        raise ContentCreatorError(str(exc)) from exc

    asset.status = AssetStatus.generating
    await session.flush()

    try:
        result = await _run_generation(
            session,
            asset=asset,
            campaign=campaign,
            plan=plan,
            copywriting_tool=copywriting_tool,
            seo_tool=seo_tool,
        )
    except Exception:
        # Revert so the 'regenerate' action can rerun against the same row.
        asset.status = AssetStatus.requested
        await session.flush()
        raise

    await _maybe_submit_for_approval(session, campaign=campaign)
    return result


async def _run_generation(
    session: AsyncSession,
    *,
    asset: ContentAsset,
    campaign: Campaign,
    plan: AssetPlan,
    copywriting_tool: CopywritingTool,
    seo_tool: SeoAnalysisTool,
) -> dict[str, Any]:
    voice = await _load_active_voice(session, tenant_id=campaign.tenant_id)
    voice_prompt = format_brand_voice_prompt(voice) if voice is not None else None

    audience = await _load_audience(session, campaign_id=campaign.id)
    audience_summary = _summarise_audience(audience)

    siblings = await _channel_sibling_index(
        session, campaign_id=campaign.id, asset=asset
    )
    target_keywords = _target_keywords(campaign)

    cw_inputs = build_copywriting_inputs(
        plan=plan,
        campaign_brief=campaign.brief,
        campaign_objective=campaign.objective,
        audience_summary=audience_summary,
        voice_prompt=voice_prompt,
        touchpoint_position=siblings["position"],
        total_touchpoints_for_channel=siblings["total"],
        target_keywords=target_keywords if plan.requires_seo else None,
        seed=str(asset.id),
    )
    cw_output = await copywriting_tool.call(cw_inputs)

    body_text = str(cw_output.get("body", ""))
    dont_words = list(voice.dont_words) if voice is not None else []
    brand_hits = check_dont_words(body_text, dont_words)
    brand_check = {
        "pass": not brand_hits,
        "failing_words": brand_hits,
        "dont_words_checked": dont_words,
    }

    seo_payload: dict[str, Any] | None = None
    if plan.requires_seo:
        if not target_keywords:
            seo_payload = {"reason": "no_target_keywords", "score": None}
        else:
            seo_payload = await seo_tool.call(
                {
                    "draft": body_text,
                    "target_keywords": target_keywords,
                    "title": cw_output.get("title")
                    or cw_output.get("headline")
                    or "",
                }
            )

    asset.title = extract_title(plan, cw_output)
    asset.content = body_text or None
    asset.extra_metadata = {
        **asset.extra_metadata,
        **bundle_metadata(
            copywriting_output=cw_output,
            brand_check=brand_check,
            seo=seo_payload,
        ),
    }
    asset.status = AssetStatus.drafted
    await session.flush()

    return {
        "asset_id": str(asset.id),
        "asset_type": asset.asset_type.value,
        "status": asset.status.value,
        "brand_check_pass": brand_check["pass"],
        "length_warning": cw_output.get("length_warning"),
        "seo_score": (seo_payload or {}).get("score") if seo_payload else None,
    }


async def _maybe_submit_for_approval(
    session: AsyncSession, *, campaign: Campaign
) -> None:
    """If every required asset is now drafted (or past), apply the
    submit_for_approval transition. Idempotent — no-op once the campaign has
    already moved on."""
    if campaign.status != CampaignStatus.content_in_production:
        return

    blocking = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
                ContentAsset.status.in_(
                    [
                        AssetStatus.requested,
                        AssetStatus.generating,
                        AssetStatus.failed,
                    ]
                ),
            )
        )
    ).first()
    if blocking is not None:
        return

    # Import here to dodge a top-level circular: state_machine imports
    # ContentAsset (via models) and we live inside an agent module.
    from app.orchestrator.state_machine import (
        GuardFailedError,
        UnknownTransitionError,
        campaign_sm,
    )

    try:
        await campaign_sm.apply(session, campaign, "submit_for_approval")
    except (UnknownTransitionError, GuardFailedError):
        # Either the campaign already moved past this state, or another
        # concurrent task already drove the transition. Both are fine.
        return


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _load_active_voice(
    session: AsyncSession, *, tenant_id: UUID
) -> BrandVoice | None:
    return (
        await session.execute(
            select(BrandVoice).where(
                BrandVoice.tenant_id == tenant_id,
                BrandVoice.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _load_audience(
    session: AsyncSession, *, campaign_id: UUID
) -> Audience | None:
    return (
        await session.execute(
            select(Audience)
            .where(Audience.campaign_id == campaign_id)
            .order_by(Audience.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _summarise_audience(audience: Audience | None) -> str:
    if audience is None:
        return ""
    return f"audience '{audience.name}' (criteria: {audience.segment_criteria})"


def _target_keywords(campaign: Campaign) -> list[str]:
    """Read target keywords from campaign.kpi_targets — convention used by
    the planner and surfaced through the brief form. Empty list is OK; the
    SEO tool then returns a `no_target_keywords` marker."""
    raw = campaign.kpi_targets.get("target_keywords") if campaign.kpi_targets else None
    if not isinstance(raw, list):
        return []
    return [str(k).strip() for k in raw if isinstance(k, str) and k.strip()]


async def _channel_sibling_index(
    session: AsyncSession, *, campaign_id: UUID, asset: ContentAsset
) -> dict[str, int]:
    """Find (position, total) for this asset within its same-channel siblings,
    used in the prompt so the model knows whether this is the first or last
    touch in the sequence."""
    same_channel = (
        await session.execute(
            select(ContentAsset.id, ContentAsset.scheduled_at)
            .where(
                ContentAsset.campaign_id == campaign_id,
                ContentAsset.asset_type == asset.asset_type,
            )
            .order_by(ContentAsset.scheduled_at.asc(), ContentAsset.created_at.asc())
        )
    ).all()
    ids = [row.id for row in same_channel]
    try:
        idx = ids.index(asset.id) + 1
    except ValueError:
        idx = 1
    return {"position": idx, "total": max(len(ids), 1)}


__all__ = [
    "ContentCreatorError",
    "ensure_content_creator_agent",
    "generate_asset",
    "seed_assets_for_campaign",
]
