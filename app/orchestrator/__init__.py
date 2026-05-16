"""Marketing Orchestrator: durable task queue, worker loop, handler registry,
campaign state machine."""

from app.orchestrator.queue import (
    claim_next,
    complete,
    enqueue_task,
    fail,
    reap_expired_leases,
)
from app.orchestrator.registry import HandlerFn, get_handler, register_handler
from app.orchestrator.state_machine import (
    GuardFailedError,
    StateMachine,
    StateMachineError,
    Transition,
    UnknownTransitionError,
    campaign_sm,
)

__all__ = [
    "GuardFailedError",
    "HandlerFn",
    "StateMachine",
    "StateMachineError",
    "Transition",
    "UnknownTransitionError",
    "campaign_sm",
    "claim_next",
    "complete",
    "enqueue_task",
    "fail",
    "get_handler",
    "reap_expired_leases",
    "register_handler",
]
