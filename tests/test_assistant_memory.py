"""W42.1 — Assistant conversation memory.

Covers round-tripping the message list through the DB, trim semantics,
and the clear flow."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.assistant.memory import (
    MAX_MESSAGES,
    _trim,
    clear_history,
    load_history,
    save_history,
    serialize_blocks,
)
from app.db.enums import UserRole
from app.db.models import AppUser, Tenant


async def _seed_user(db_engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"mem-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            email=f"u-{uuid.uuid4().hex[:6]}@mem.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        return user.id, tenant.id


def _user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant_msg(text: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


async def test_round_trip_via_db(db_engine: AsyncEngine) -> None:
    user_id, tenant_id = await _seed_user(db_engine)
    history = [_user_msg("hi"), _assistant_msg("hello there")]
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await save_history(
            session, user_id=user_id, tenant_id=tenant_id, messages=history
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        loaded = await load_history(session, user_id=user_id)
    assert loaded == history


async def test_load_returns_empty_when_no_row(db_engine: AsyncEngine) -> None:
    user_id, _ = await _seed_user(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        loaded = await load_history(session, user_id=user_id)
    assert loaded == []


async def test_save_overwrites(db_engine: AsyncEngine) -> None:
    user_id, tenant_id = await _seed_user(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await save_history(
            session, user_id=user_id, tenant_id=tenant_id,
            messages=[_user_msg("v1")],
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await save_history(
            session, user_id=user_id, tenant_id=tenant_id,
            messages=[_user_msg("v2")],
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        loaded = await load_history(session, user_id=user_id)
    assert loaded == [_user_msg("v2")]


async def test_clear_wipes(db_engine: AsyncEngine) -> None:
    user_id, tenant_id = await _seed_user(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await save_history(
            session, user_id=user_id, tenant_id=tenant_id,
            messages=[_user_msg("forgettable")],
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await clear_history(session, user_id=user_id)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        loaded = await load_history(session, user_id=user_id)
    assert loaded == []


# ---------------------------------------------------------------------------
# Trim semantics
# ---------------------------------------------------------------------------


def test_trim_does_nothing_when_under_cap() -> None:
    msgs = [_user_msg(f"u{i}") for i in range(MAX_MESSAGES)]
    assert _trim(msgs) == msgs


def test_trim_keeps_newest_messages() -> None:
    # Build a clean alternating chat that exceeds the cap.
    msgs: list[dict[str, Any]] = []
    for i in range(MAX_MESSAGES + 10):
        msgs.append(_user_msg(f"u{i}"))
        msgs.append(_assistant_msg(f"a{i}"))
    trimmed = _trim(msgs)
    assert len(trimmed) <= MAX_MESSAGES
    # Last message preserved.
    assert trimmed[-1] == msgs[-1]
    # First message starts with a user role (never an orphan assistant).
    assert trimmed[0].get("role") == "user"


def test_trim_drops_orphan_tool_result() -> None:
    """A tool_result without its matching tool_use must be dropped to
    avoid Anthropic rejecting the next request."""
    msgs: list[dict[str, Any]] = []
    for i in range(MAX_MESSAGES):
        msgs.append(_user_msg(f"u{i}"))
        msgs.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"tu-{i}", "name": "x", "input": {}}],
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"tu-{i}", "content": "ok"}],
            }
        )
    trimmed = _trim(msgs)
    # No leading tool_result with an unknown tool_use_id.
    known_ids: set[str] = set()
    for m in trimmed:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    known_ids.add(block["id"])
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    assert block["tool_use_id"] in known_ids


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_serialize_blocks_handles_sdk_text_block() -> None:
    from anthropic.types import TextBlock

    blocks = [TextBlock(type="text", text="hi", citations=None)]
    out = serialize_blocks(blocks)
    assert out[0]["text"] == "hi"
    assert out[0]["type"] == "text"


def test_serialize_blocks_handles_tool_use() -> None:
    from anthropic.types import ToolUseBlock

    blocks = [ToolUseBlock(type="tool_use", id="tu-1", name="list_campaigns", input={})]
    out = serialize_blocks(blocks)
    assert out[0]["id"] == "tu-1"
    assert out[0]["name"] == "list_campaigns"


def test_serialize_blocks_passes_through_plain_dicts() -> None:
    out = serialize_blocks([{"type": "text", "text": "raw"}])
    assert out == [{"type": "text", "text": "raw"}]
