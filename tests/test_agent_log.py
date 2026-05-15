"""W5: agent_log writer + append-only enforcement."""

import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents.log import agent_log_emit
from app.db.enums import AgentKind
from app.db.models import Agent, AgentLog, Tenant


async def _make_agent(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = Tenant(name=f"agt-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    agent = Agent(
        tenant_id=tenant.id,
        name=f"agent-{uuid.uuid4().hex[:8]}",
        agent_type=AgentKind.orchestrator,
    )
    session.add(agent)
    await session.flush()
    return tenant.id, agent.id


async def test_agent_log_emit_writes_row(db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tenant_id, agent_id = await _make_agent(session)
        await agent_log_emit(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            task_id=None,
            action="test.invocation",
            log_data={"latency_ms": 42, "tool_calls": ["echo"]},
            severity="info",
        )
        await session.commit()

    async with AsyncSession(db_engine, expire_on_commit=False) as q:
        result = await q.execute(select(AgentLog).where(AgentLog.agent_id == agent_id))
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.action == "test.invocation"
    assert row.log_data == {"latency_ms": 42, "tool_calls": ["echo"]}
    assert row.severity == "info"
    assert row.tenant_id == tenant_id


async def test_mas_app_cannot_update_agent_log(db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        tenant_id, agent_id = await _make_agent(setup)
        await agent_log_emit(
            setup,
            tenant_id=tenant_id,
            agent_id=agent_id,
            task_id=None,
            action="probe",
        )
        await setup.commit()

    async with AsyncSession(db_engine, expire_on_commit=False) as scoped, scoped.begin():
        await scoped.execute(text("SET LOCAL ROLE mas_app"))
        await scoped.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            await scoped.execute(
                update(AgentLog).where(AgentLog.agent_id == agent_id).values(action="rewritten")
            )
