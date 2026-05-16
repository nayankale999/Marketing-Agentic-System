"""Shared agent tools (SEO, copywriting, A/B, segmentation, social, email).

W8 ships the base class + registry + two stub tools. Real tools land per E11.
"""

from app.tools._stubs import EchoTool, FlakyTool
from app.tools.base import Tool, ToolRegistry, tool_registry

_REGISTERED = False


def register_builtin_tools() -> None:
    """Populate the global tool registry. Idempotent."""
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    tool_registry.register(EchoTool())
    tool_registry.register(FlakyTool())


__all__ = [
    "EchoTool",
    "FlakyTool",
    "Tool",
    "ToolRegistry",
    "register_builtin_tools",
    "tool_registry",
]
