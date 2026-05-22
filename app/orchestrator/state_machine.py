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
from app.db.enums import AgentKind, CampaignStatus
from app.db.models import Agent, Campaign, StrategyProposal
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
