"""W42 — Assistant router + tools.

Anthropic is stubbed so we don't burn tokens on every test run. We
verify the dispatch logic, RBAC enforcement, and confirmation flow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.assistant.router import (
    AssistantError,
    handle_message,
    ToolUnavailableError,
)
from app.assistant.tools import ToolPermissionError
from app.db.enums import CampaignStatus, CampaignType, UserRole
from app.db.models import AppUser, Campaign, Tenant


# ---------------------------------------------------------------------------
# Fake Anthropic responses
# ---------------------------------------------------------------------------


def _block(*, type_: str, **kwargs):
    """Mimic an Anthropic content block."""
    return SimpleNamespace(__class__=_fake_class_for(type_), **kwargs)


def _fake_class_for(name: str):
    """Return a fake class object whose isinstance checks match the
    block type Claude returns. The real router uses
    `isinstance(block, ToolUseBlock)` / `TextBlock`, so we use those
    real classes when building stubs."""
    from anthropic.types import TextBlock, ToolUseBlock

    return {"text": TextBlock, "tool_use": ToolUseBlock}[name]


def text_block(s: str):
    from anthropic.types import TextBlock

    return TextBlock(type="text", text=s, citations=None)


def tool_use_block(*, name: str, tool_id: str = "tu-1", input_: dict[str, Any] | None = None):
    from anthropic.types import ToolUseBlock

    return ToolUseBlock(type="tool_use", id=tool_id, name=name, input=input_ or {})


def fake_message(content_blocks: list, *, in_tokens: int = 50, out_tokens: int = 30):
    return SimpleNamespace(
        content=content_blocks,
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


def fake_client(responses: list):
    """Returns a fake AsyncAnthropic-like client whose `messages.create`
    pops one queued response per call."""
    client = SimpleNamespace(messages=SimpleNamespace())
    create_mock = AsyncMock(side_effect=responses)
    client.messages.create = create_mock
    return client


# ---------------------------------------------------------------------------
# World seeding
# ---------------------------------------------------------------------------


async def _seed(db_engine: AsyncEngine, role: UserRole = UserRole.marketer) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ast-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            email=f"u-{uuid.uuid4().hex[:6]}@ast.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=user.id,
            name="The Q3 Push",
            campaign_type=CampaignType.demand_gen,
            objective="o",
            budget_total=Decimal("1000"),
            currency="USD",
            start_date=date.today() - timedelta(days=5),
            end_date=date.today() + timedelta(days=20),
            brief="b",
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "user": user,
            "campaign_id": campaign.id,
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_router_dispatches_simple_text_response(db_engine: AsyncEngine) -> None:
    """If Claude returns text-only (no tool_use), we surface that text."""
    world = await _seed(db_engine)
    user = world["user"]
    client = fake_client([fake_message([text_block("Hello there!")])])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await handle_message(
            session=session,
            user=user,
            message="hi",
            client=client,
            model="claude-test",
        )
    assert result.text == "Hello there!"
    assert result.tool_name is None
    assert result.turns == 1
    assert result.input_tokens > 0


async def test_router_dispatches_tool_then_summarises(db_engine: AsyncEngine) -> None:
    """Claude calls list_campaigns, then on the follow-up turn gives
    a text reply incorporating the tool result."""
    world = await _seed(db_engine)
    user = world["user"]
    client = fake_client(
        [
            fake_message([tool_use_block(name="list_campaigns", input_={})]),
            fake_message([text_block("Here are your campaigns.")]),
        ]
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await handle_message(
            session=session,
            user=user,
            message="show campaigns",
            client=client,
            model="claude-test",
        )
    assert result.tool_name == "list_campaigns"
    assert "Here are your campaigns" in result.text
    assert result.turns == 2
    # Tool data surfaced through.
    assert "campaigns" in result.tool_data
    assert any(c["name"] == "The Q3 Push" for c in result.tool_data["campaigns"])


async def test_destructive_tool_requires_confirmation(db_engine: AsyncEngine) -> None:
    """First pause_campaign call (without confirm) short-circuits; we don't
    go back to Claude for a follow-up turn."""
    world = await _seed(db_engine, role=UserRole.manager)
    user = world["user"]
    client = fake_client(
        [
            fake_message(
                [tool_use_block(name="pause_campaign", input_={"identifier": "Q3 Push"})]
            )
        ]
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await handle_message(
            session=session,
            user=user,
            message="pause Q3 push",
            client=client,
            model="claude-test",
        )
    assert result.requires_confirmation is True
    assert "Confirm" in result.tool_summary
    # Campaign still live.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        c = await session.get(Campaign, world["campaign_id"])
        assert c.status == CampaignStatus.live


async def test_destructive_tool_with_confirm_executes(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine, role=UserRole.manager)
    user = world["user"]
    client = fake_client(
        [
            fake_message(
                [
                    tool_use_block(
                        name="pause_campaign",
                        input_={"identifier": "Q3 Push", "confirm": True},
                    )
                ]
            ),
            fake_message([text_block("Paused.")]),
        ]
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await handle_message(
            session=session,
            user=user,
            message="yes, pause it",
            client=client,
            model="claude-test",
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        c = await session.get(Campaign, world["campaign_id"])
        assert c.status == CampaignStatus.paused


async def test_router_surfaces_permission_error_back_to_claude(
    db_engine: AsyncEngine,
) -> None:
    """Viewer trying to pause — the tool raises ToolPermissionError;
    the router feeds the error back to Claude which then narrates."""
    world = await _seed(db_engine, role=UserRole.viewer)
    user = world["user"]
    client = fake_client(
        [
            fake_message(
                [tool_use_block(name="pause_campaign", input_={"identifier": "Q3 Push"})]
            ),
            fake_message([text_block("You don't have permission to pause campaigns.")]),
        ]
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await handle_message(
            session=session,
            user=user,
            message="pause it",
            client=client,
            model="claude-test",
        )
    assert "permission" in result.text.lower()


async def test_router_unknown_tool_raises(db_engine: AsyncEngine) -> None:
    world = await _seed(db_engine)
    user = world["user"]
    client = fake_client(
        [fake_message([tool_use_block(name="banana_fries", input_={})])]
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(ToolUnavailableError):
            await handle_message(
                session=session,
                user=user,
                message="nonsense",
                client=client,
                model="claude-test",
            )


async def test_router_aborts_after_turn_cap(db_engine: AsyncEngine) -> None:
    """Stuck loop: Claude keeps issuing tool_use without ever replying."""
    world = await _seed(db_engine)
    user = world["user"]
    client = fake_client(
        [
            fake_message([tool_use_block(name="list_campaigns", tool_id=f"t{i}")])
            for i in range(10)
        ]
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        with pytest.raises(AssistantError):
            await handle_message(
                session=session,
                user=user,
                message="loop me",
                client=client,
                model="claude-test",
            )
