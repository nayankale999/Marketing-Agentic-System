"""Shared agent tools (SEO, copywriting, A/B, segmentation, social, email).

W8 ships the base class + registry + two stub tools. Real tools land per E11.
Copywriting (W17, E11-S02) registers only when ANTHROPIC_API_KEY is configured —
without a key we'd hit a 401 on the first call, so a missing tool is preferable
to a broken one.
"""

from anthropic import AsyncAnthropic

from app.settings.config import get_settings
from app.tools._stubs import EchoTool, FlakyTool
from app.tools.base import Tool, ToolRegistry, tool_registry
from app.tools.copywriting import CopywritingTool

_REGISTERED = False


def register_builtin_tools() -> None:
    """Populate the global tool registry. Idempotent."""
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    tool_registry.register(EchoTool())
    tool_registry.register(FlakyTool())

    settings = get_settings()
    if settings.anthropic_api_key:
        tool_registry.register(
            CopywritingTool(
                client=AsyncAnthropic(api_key=settings.anthropic_api_key),
                model=settings.copywriting_model,
            )
        )


__all__ = [
    "CopywritingTool",
    "EchoTool",
    "FlakyTool",
    "Tool",
    "ToolRegistry",
    "register_builtin_tools",
    "tool_registry",
]
