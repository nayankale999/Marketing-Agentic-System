"""Built-in skill handlers. Import this package to register them.

Workers and tests call `register_builtin_handlers()` to populate the global
handler registry. Idempotent.
"""

from app.orchestrator.handlers.audience import audience_materialise_handler
from app.orchestrator.handlers.content_creator import (
    content_creator_generate_asset_handler,
)
from app.orchestrator.handlers.distribution import (
    distribution_dispatch_email_handler,
    distribution_dispatch_social_handler,
)
from app.orchestrator.handlers.echo import echo_handler
from app.orchestrator.handlers.strategist import campaign_strategist_propose_handler
from app.orchestrator.handlers.tool import register_tool_handlers
from app.orchestrator.registry import register_handler
from app.tools import register_builtin_tools

_REGISTERED = False


def register_builtin_handlers() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    register_handler("echo", echo_handler)
    register_handler("audience_targeting.materialise", audience_materialise_handler)
    register_handler("campaign_strategist.propose", campaign_strategist_propose_handler)
    register_handler(
        "content_creator.generate_asset", content_creator_generate_asset_handler
    )
    register_handler(
        "distribution.dispatch_email", distribution_dispatch_email_handler
    )
    register_handler(
        "distribution.dispatch_social", distribution_dispatch_social_handler
    )
    # Tools must be registered before their handlers can be built.
    register_builtin_tools()
    register_tool_handlers()


__all__ = [
    "audience_materialise_handler",
    "campaign_strategist_propose_handler",
    "content_creator_generate_asset_handler",
    "distribution_dispatch_email_handler",
    "distribution_dispatch_social_handler",
    "echo_handler",
    "register_builtin_handlers",
    "register_tool_handlers",
]
