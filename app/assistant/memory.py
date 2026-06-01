"""Conversation memory for the dashboard assistant (W42.1).

The Anthropic SDK returns rich block objects (TextBlock + ToolUseBlock).
For storage we serialise them to plain dicts. For sending back to
Claude we re-shape into the SDK's message structure.

The application-layer trim cap (`MAX_MESSAGES`) keeps the input token
cost bounded. We trim FROM THE FRONT — newest messages are always
preserved — but we never split a tool_use / tool_result pair, because
Claude rejects messages that reference a tool_use_id it can't find.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AssistantConversation


# 20 message entries = roughly 10 user/assistant turn pairs. Bigger windows
# inflate token cost; smaller ones lose useful context.
MAX_MESSAGES = 20


async def load_history(
    session: AsyncSession, *, user_id: UUID
) -> list[dict[str, Any]]:
    """Pull the persisted message list. Returns `[]` if the user has
    no prior conversation."""
    row = await session.get(AssistantConversation, user_id)
    if row is None:
        return []
    return list(row.messages or [])


async def save_history(
    session: AsyncSession,
    *,
    user_id: UUID,
    tenant_id: UUID,
    messages: list[dict[str, Any]],
) -> None:
    """Upsert the message list, trimming to MAX_MESSAGES first."""
    trimmed = _trim(messages)
    row = await session.get(AssistantConversation, user_id)
    if row is None:
        row = AssistantConversation(
            user_id=user_id,
            tenant_id=tenant_id,
            messages=trimmed,
        )
        session.add(row)
    else:
        row.messages = trimmed
        row.updated_at = datetime.now(UTC)
    await session.flush()


async def clear_history(
    session: AsyncSession, *, user_id: UUID
) -> None:
    """Wipe the user's conversation. Used by the 'Start over' button."""
    row = await session.get(AssistantConversation, user_id)
    if row is not None:
        row.messages = []
        # Active campaign focus is part of "the conversation" — clearing
        # wipes it too. Persistent across normal turns but reset on Start
        # over, matching what the user expects.
        row.active_campaign_id = None
        row.updated_at = datetime.now(UTC)
        await session.flush()


async def get_active_campaign(
    session: AsyncSession, *, user_id: UUID
) -> UUID | None:
    """Return the campaign the user was most recently working on with
    the assistant, or None if no row / not set."""
    row = await session.get(AssistantConversation, user_id)
    return row.active_campaign_id if row is not None else None


async def set_active_campaign(
    session: AsyncSession,
    *,
    user_id: UUID,
    tenant_id: UUID,
    campaign_id: UUID | None,
) -> None:
    """Set the active campaign pointer. Idempotent. Creates the row if
    no conversation exists yet (rare — first turn would normally have
    triggered save_history first)."""
    row = await session.get(AssistantConversation, user_id)
    if row is None:
        row = AssistantConversation(
            user_id=user_id,
            tenant_id=tenant_id,
            messages=[],
            active_campaign_id=campaign_id,
        )
        session.add(row)
    else:
        row.active_campaign_id = campaign_id
        row.updated_at = datetime.now(UTC)
    await session.flush()


def _trim(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the oldest messages until we're at MAX_MESSAGES.

    Invariants we keep:
      1. First message has role 'user' (Anthropic rejects an opening
         assistant turn).
      2. No 'tool_result' block references a tool_use_id that isn't
         present earlier in the slice.

    We slice from the right then peel from the front until both
    invariants hold.
    """
    if len(messages) <= MAX_MESSAGES:
        return messages
    candidate = messages[-MAX_MESSAGES:]

    while candidate:
        first = candidate[0]
        # Invariant #1 — leading message must be a user message.
        if first.get("role") != "user":
            candidate = candidate[1:]
            continue
        # Invariant #2 — any tool_result must have its tool_use earlier
        # in the slice. Rebuild the known set on each pass: peeling can
        # remove a tool_use that an earlier pass relied on.
        known_tool_use_ids: set[str] = set()
        for m in candidate:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id")
                        if tid:
                            known_tool_use_ids.add(tid)
        first_content = first.get("content")
        dangling = False
        if isinstance(first_content, list):
            for block in first_content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tu_id = block.get("tool_use_id")
                    if tu_id and tu_id not in known_tool_use_ids:
                        dangling = True
                        break
        if dangling:
            candidate = candidate[1:]
            continue
        break

    return candidate


def serialize_blocks(blocks: Any) -> list[dict[str, Any]]:
    """Convert SDK content blocks (TextBlock / ToolUseBlock) to plain
    dicts suitable for JSON storage AND for re-sending to Claude on a
    later turn."""
    out: list[dict[str, Any]] = []
    for b in blocks or []:
        # Pydantic model from the SDK — `.model_dump()` round-trips.
        if hasattr(b, "model_dump"):
            out.append(b.model_dump(mode="json"))
        elif isinstance(b, dict):
            out.append(b)
        # Anything else we silently drop; the SDK never emits other shapes.
    return out


__all__ = [
    "MAX_MESSAGES",
    "load_history",
    "save_history",
    "clear_history",
    "serialize_blocks",
    "get_active_campaign",
    "set_active_campaign",
]
