"""Built-in skill handlers. Import this package to register them.

Workers and tests call `register_builtin_handlers()` to populate the global
handler registry. Idempotent.
"""

from app.orchestrator.handlers.echo import echo_handler
from app.orchestrator.registry import register_handler

_REGISTERED = False


def register_builtin_handlers() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    register_handler("echo", echo_handler)


__all__ = ["echo_handler", "register_builtin_handlers"]
