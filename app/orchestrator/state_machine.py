"""Campaign state machine.

Transitions are declarative: each one binds a (from_state, name) pair to a
to_state, plus optional `guard` and `on_enter` callbacks. The full lifecycle
from `architecture.md` will land as later work units wire up the specialist
agents; for Slice 1 we register a single placeholder transition,
`echo_step: drafted -> drafted`, whose on_enter enqueues an echo task. That
proves the orchestrator -> queue -> worker loop end-to-end without needing
real specialists.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import write_audit
from app.db.enums import AgentKind, AssetStatus, CampaignStatus
from app.db.models import Agent, Campaign, ContentAsset, StrategyProposal
from app.orchestrator.queue import enqueue_task

Guard = Callable[[AsyncSession, Campaign], Awaitable[bool]]
OnEnter = Callable[[AsyncSession, Campaign], Awaitable[None]]


class StateMachineError(Exception):
    """Base class for state-machine misuse."""


class UnknownTransitionError(StateMachineError):
    """No transition with this name from the campaign's current state."""


class GuardFailedError(StateMachineError):
    """The transition exists but its guard returned False."""


@dataclass(frozen=True)
class Transition:
    name: str
    from_state: CampaignStatus
    to_state: CampaignStatus
    guard: Guard | None = None
    on_enter: OnEnter | None = None


class StateMachine:
    def __init__(self) -> None:
        self._transitions: dict[tuple[CampaignStatus, str], Transition] = {}

    def register(self, transition: Transition) -> None:
        key = (transition.from_state, transition.name)
        if key in self._transitions:
            raise ValueError(
                f"transition '{transition.name}' from {transition.from_state.value} "
                "is already registered",
            )
        self._transitions[key] = transition

    def transitions_from(self, state: CampaignStatus) -> list[str]:
        return sorted(name for (from_state, name) in self._transitions if from_state == state)

    async def apply(
        self,
        session: AsyncSession,
        campaign: Campaign,
        transition_name: str,
    ) -> Campaign:
        """Apply a transition. Caller is responsible for the surrounding tx.

        Raises `UnknownTransitionError` if no transition with this name is registered
        for the campaign's current status, or `GuardFailedError` if the transition's
        guard rejects.
        """
        transition = self._transitions.get((campaign.status, transition_name))
        if transition is None:
            raise UnknownTransitionError(
                f"no transition '{transition_name}' from status '{campaign.status.value}'; "
                f"allowed: {self.transitions_from(campaign.status)}"
            )
        if transition.guard is not None and not await transition.guard(session, campaign):
            raise GuardFailedError(
                f"transition '{transition_name}' from '{campaign.status.value}' rejected by guard"
            )

        from_state = campaign.status
        campaign.status = transition.to_state
        await session.flush()

        write_audit(
            session,
            tenant_id=campaign.tenant_id,
            actor_kind=current_actor_kind.get(),
            actor_id=current_actor_id.get(),
            entity_kind="campaign",
            entity_id=campaign.id,
            action="transition_applied",
            before_state={"status": from_state.value},
            after_state={"status": transition.to_state.value},
            metadata={"transition": transition.name},
        )

        if transition.on_enter is not None:
            await transition.on_enter(session, campaign)

        return campaign


# -----------------------------------------------------------------------------
# Built-in transitions (singleton state machine)
# -----------------------------------------------------------------------------

campaign_sm = StateMachine()


async def _ensure_orchestrator_agent(session: AsyncSession, tenant_id: UUID) -> Agent:
    """Get-or-create the per-tenant Marketing Orchestrator agent row.

    Specialist agents will be created the same way as their work units land.
    """
    existing = (
        await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == AgentKind.orchestrator,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    agent = Agent(
        tenant_id=tenant_id,
        name="Marketing Orchestrator",
        agent_type=AgentKind.orchestrator,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _enqueue_echo_step(session: AsyncSession, campaign: Campaign) -> None:
    """Slice-1 placeholder: enqueue an echo task on every echo_step transition."""
    agent = await _ensure_orchestrator_agent(session, campaign.tenant_id)
    await enqueue_task(
        session,
        tenant_id=campaign.tenant_id,
        agent_id=agent.id,
        campaign_id=campaign.id,
        skill_name="echo",
        input_data={
            "campaign_id": str(campaign.id),
            "transition": "echo_step",
        },
    )


campaign_sm.register(
    Transition(
        name="echo_step",
        from_state=CampaignStatus.drafted,
        to_state=CampaignStatus.drafted,
        on_enter=_enqueue_echo_step,
    )
)


async def _has_accepted_strategy(session: AsyncSession, campaign: Campaign) -> bool:
    """Guard for `set_strategy`: a strategy proposal must be accepted before
    we leave `audience_built`. The accept endpoint flips the flag in the same
    transaction, so the guard sees it on the subsequent transition call."""
    accepted = (
        await session.execute(
            select(StrategyProposal.id).where(
                StrategyProposal.campaign_id == campaign.id,
                StrategyProposal.is_accepted.is_(True),
            )
        )
    ).first()
    return accepted is not None


campaign_sm.register(
    Transition(
        name="set_strategy",
        from_state=CampaignStatus.audience_built,
        to_state=CampaignStatus.strategy_set,
        guard=_has_accepted_strategy,
    )
)


async def _seed_content_assets(session: AsyncSession, campaign: Campaign) -> None:
    """on_enter for `start_content`: build the content_asset rows + enqueue
    one generation task per row. Lives here (rather than in the route) so
    the same effect fires whether the transition was triggered by the API
    or by the orchestrator (e.g. an auto-advance later)."""
    # Imported here to avoid a top-level circular: content_creator imports the
    # state machine in its post-draft hook.
    from app.agents.content_creator import seed_assets_for_campaign

    await seed_assets_for_campaign(session, campaign=campaign)


campaign_sm.register(
    Transition(
        name="start_content",
        from_state=CampaignStatus.strategy_set,
        to_state=CampaignStatus.content_in_production,
        on_enter=_seed_content_assets,
    )
)


async def _all_required_assets_drafted(
    session: AsyncSession, campaign: Campaign
) -> bool:
    """Guard for `submit_for_approval`: no required content_asset is still in
    a pre-drafted or failed state, AND no required asset is blocked by a
    critical compliance hit (W23, E06-S08 #2). Empty asset set blocks — you
    can't move to approval without any content."""
    total_required = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
            )
        )
    ).first()
    if total_required is None:
        return False

    state_blocking = (
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
    if state_blocking is not None:
        return False

    # E06-S08 #2: critical-severity compliance hits block auto-promotion
    # until a manager explicitly clears them via /clear-compliance.
    compliance_blocking = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
                ContentAsset.extra_metadata["compliance"]["blocked"].astext == "true",
            )
        )
    ).first()
    return compliance_blocking is None


async def _flip_drafted_assets_to_pending_approval(
    session: AsyncSession, campaign: Campaign
) -> None:
    """on_enter for `submit_for_approval` (W25, E07-S01 + W26, E07-S04 #3):
    move every required drafted asset into `pending_approval` and snapshot
    the tenant's approval-gate thresholds onto each asset.

    The snapshot is the load-bearing piece for E07-S04 #3: 'in-flight
    approvals continue under the threshold at submission time'. Approve
    endpoints read the snapshot, not the live tenant_approval_settings row.
    A regenerate→re-submit cycle captures a fresh snapshot at the next
    submission (correct, intentional)."""
    from datetime import UTC, datetime  # local import — only used here

    from app.db.models import TenantApprovalSettings

    rows = (
        await session.execute(
            select(ContentAsset).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
                ContentAsset.status == AssetStatus.drafted,
            )
        )
    ).scalars().all()
    if not rows:
        return

    settings_row = (
        await session.execute(
            select(TenantApprovalSettings).where(
                TenantApprovalSettings.tenant_id == campaign.tenant_id
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    snapshot: dict[str, object] = {
        "admin_required_above_amount": (
            str(settings_row.admin_required_above_amount)
            if settings_row and settings_row.admin_required_above_amount is not None
            else None
        ),
        "auto_approval_cap_amount": (
            str(settings_row.auto_approval_cap_amount) if settings_row else "0"
        ),
        "currency": settings_row.currency if settings_row else "USD",
        "snapshot_taken_at": now.isoformat(),
    }
    for row in rows:
        row.status = AssetStatus.pending_approval
        row.updated_at = now
        row.extra_metadata = {
            **(row.extra_metadata or {}),
            "approval_threshold": snapshot,
        }
    await session.flush()


campaign_sm.register(
    Transition(
        name="submit_for_approval",
        from_state=CampaignStatus.content_in_production,
        to_state=CampaignStatus.approval_pending,
        guard=_all_required_assets_drafted,
        on_enter=_flip_drafted_assets_to_pending_approval,
    )
)


async def _all_required_assets_approved(
    session: AsyncSession, campaign: Campaign
) -> bool:
    """Guard for `start_launch` (W25): every required asset must be `approved`.
    Empty asset set is rejected — you can't launch a campaign with no content."""
    total_required = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
            )
        )
    ).first()
    if total_required is None:
        return False

    not_yet_approved = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
                ContentAsset.status != AssetStatus.approved,
            )
        )
    ).first()
    return not_yet_approved is None


async def _schedule_approved_assets(
    session: AsyncSession, campaign: Campaign
) -> None:
    """on_enter for `start_launch` (W28, E08-S01): every approved asset gets
    its slot from the touchpoint calendar and a dispatch task enqueued.
    Imported lazily because distribution imports the state machine for its
    `_maybe_go_live` hook."""
    from app.agents.distribution import schedule_approved_assets

    await schedule_approved_assets(session, campaign=campaign)


campaign_sm.register(
    Transition(
        name="start_launch",
        from_state=CampaignStatus.approval_pending,
        to_state=CampaignStatus.ready_to_launch,
        guard=_all_required_assets_approved,
        on_enter=_schedule_approved_assets,
    )
)


async def _ready_to_go_live(
    session: AsyncSession, campaign: Campaign
) -> bool:
    """Guard for `start_live` (W28): start_date must be reached AND no
    required asset is still in pre-scheduled state. Assets already in
    `published` count as 'past scheduling' — fine to advance."""
    from datetime import UTC, datetime

    today = datetime.now(UTC).date()
    if campaign.start_date > today:
        return False

    total_required = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
            )
        )
    ).first()
    if total_required is None:
        return False

    blocking = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
                ContentAsset.status.in_(
                    [
                        AssetStatus.requested,
                        AssetStatus.generating,
                        AssetStatus.drafted,
                        AssetStatus.pending_approval,
                        AssetStatus.approved,
                        AssetStatus.rejected,
                        AssetStatus.failed,
                    ]
                ),
            )
        )
    ).first()
    return blocking is None


campaign_sm.register(
    Transition(
        name="start_live",
        from_state=CampaignStatus.ready_to_launch,
        to_state=CampaignStatus.live,
        guard=_ready_to_go_live,
    )
)


async def _any_required_asset_rejected(
    session: AsyncSession, campaign: Campaign
) -> bool:
    """Guard for `regenerate_after_rejection` (W25): at least one required
    asset is in `rejected` and needs to cycle back through content production."""
    rejected = (
        await session.execute(
            select(ContentAsset.id).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.is_required.is_(True),
                ContentAsset.status == AssetStatus.rejected,
            )
        )
    ).first()
    return rejected is not None


campaign_sm.register(
    Transition(
        name="regenerate_after_rejection",
        from_state=CampaignStatus.approval_pending,
        to_state=CampaignStatus.content_in_production,
        guard=_any_required_asset_rejected,
    )
)


async def _cancel_queued_on_pause(
    session: AsyncSession, campaign: Campaign
) -> None:
    """on_enter for `pause` (W31, E08-S07 #2). Cancels queued + awaiting_retry
    tasks for the campaign. Running tasks are intentionally left alone — the
    AC's literal reading is 'currently-executing tasks complete'."""
    from app.orchestrator.queue import cancel_queued_for_campaign

    await cancel_queued_for_campaign(session, campaign_id=campaign.id)


# Pause can fire from any active campaign state. We register one transition
# per (from_state, "pause") because the SM uses (from, name) as the key.
for _pause_from in (
    CampaignStatus.live,
    CampaignStatus.optimising,
    CampaignStatus.ready_to_launch,
):
    campaign_sm.register(
        Transition(
            name="pause",
            from_state=_pause_from,
            to_state=CampaignStatus.paused,
            on_enter=_cancel_queued_on_pause,
        )
    )


async def _resume_distribution(
    session: AsyncSession, campaign: Campaign
) -> None:
    """on_enter for `resume` (W31, E08-S07 #3). For every scheduled asset
    on the campaign:
      - elapsed slot → flip to `failed` with `skip_reason=slot_elapsed_during_pause`
      - future slot → re-enqueue a dispatch task (idempotent via W28's
        dispatch_attempt UNIQUE constraint, so duplicates are safe)
    """
    from app.agents.distribution import resume_distribution_for_campaign

    await resume_distribution_for_campaign(session, campaign=campaign)


campaign_sm.register(
    Transition(
        name="resume",
        from_state=CampaignStatus.paused,
        to_state=CampaignStatus.live,
        on_enter=_resume_distribution,
    )
)


async def _auto_generate_report(
    session: AsyncSession, campaign: Campaign
) -> None:
    """on_enter for `complete_campaign` (W38, E10-S04). Snapshots the
    final report so a regeneration is never needed to share with
    stakeholders. Late-arriving data still produces a v2 if someone hits
    the regenerate endpoint."""
    from datetime import UTC, datetime

    from app.analytics.report import generate_report

    await generate_report(
        session,
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        now=datetime.now(UTC),
        generated_by="system",
    )


# Complete can fire from any active-or-paused state. Same shape as the
# pause registrations above.
for _complete_from in (
    CampaignStatus.live,
    CampaignStatus.optimising,
    CampaignStatus.paused,
):
    campaign_sm.register(
        Transition(
            name="complete_campaign",
            from_state=_complete_from,
            to_state=CampaignStatus.completed,
            on_enter=_auto_generate_report,
        )
    )
