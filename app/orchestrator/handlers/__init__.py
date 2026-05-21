"""Built-in skill handlers. Import this package to register them.

Workers and tests call `register_builtin_handlers()` to populate the global
handler registry. Idempotent.
"""

from app.orchestrator.handlers.audience import audience_materialise_handler
from app.orchestrator.handlers.echo import echo_handler
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
    # Tools must be registered before their handlers can be built.
    register_builtin_tools()
    register_tool_handlers()


__all__ = [
    "audience_materialise_handler",
    "echo_handler",
    "register_builtin_handlers",
    "register_tool_handlers",
]
