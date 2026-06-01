"""Assistant: Claude-driven tool-use for the dashboard (W42).

The assistant is a thin layer over existing endpoints. It does NOT
duplicate business logic — every action goes through the same Python
helpers the REST API uses. The Anthropic call is purely for intent
classification + argument extraction.
"""

from app.assistant.router import (
    AssistantError,
    AssistantResult,
    ToolUnavailableError,
    handle_message,
)

__all__ = [
    "AssistantError",
    "AssistantResult",
    "ToolUnavailableError",
    "handle_message",
]
