"""W8: tool registry skeleton + agent_log emission on every invocation.

Covers:
  - EchoTool happy path -> one `tool.echo_tool.succeeded` agent_log row
  - FlakyTool fails twice then succeeds: 2 `failed` rows + 1 `succeeded`,
    all carrying the right `attempt` number, and the orchestrator's retry
    path produces the correct final task state.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.enums import AgentKind, TaskStatus
from app.db.models import Agent, AgentLog, Task, Tenant
from app.orchestrator.handlers import register_builtin_handlers
from app.orchestrator.queue import enqueue_task
from app.orchestrator.worker import run_once
from app.tools import register_builtin_tools, tool_registry

register_builtin_tools()
register_builtin_handlers()


@pytest.fixture(autouse=True)
async def _clean_task_queue(db_engine: AsyncEngine):
    # agent_log.task_id REFERENCES task(id) ON DELETE RESTRICT, so we have to
    # purge task-scoped log rows before deleting the tasks themselves.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await session.execute(text("DELETE FROM agent_log WHERE task_id IS NOT NULL"))
        await session.execute(text("DELETE FROM task"))
    yield


@pytest.fixture
async def tenant_and_agent(db_engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tenant = Tenant(name=f"tools-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        agent = Agent(
            tenant_id=tenant.id,
            name=f"orch-{uuid.uuid4().hex[:8]}",
            agent_type=AgentKind.orchestrator,
        )
        session.add(agent)
        await session.commit()
        return tenant.id, agent.id


def test_tool_registry_lists_stubs() -> None:
    assert {"echo_tool", "flaky_tool"} <= set(tool_registry.names())


async def test_echo_tool_invocation_logs_success(db_engine: AsyncEngine, tenant_and_agent) -> None:
    tenant_id, agent_id = tenant_and_agent

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="echo_tool",
            input_data={"msg": "hello"},
        )
        await session.commit()
        task_id = task.id

    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    handled = await run_once(worker_id="w-echo", session_maker=session_maker)
    assert handled is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        refreshed = await session.get(Task, task_id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.succeeded
        assert refreshed.output_data == {"msg": "hello"}

        logs = (
            (
                await session.execute(
                    select(AgentLog).where(AgentLog.task_id == task_id).order_by(AgentLog.logged_at)
                )
            )
            .scalars()
            .all()
        )

    assert len(logs) == 1
    log = logs[0]
    assert log.action == "tool.echo_tool.succeeded"
    assert log.severity == "info"
    assert log.log_data["tool"] == "echo_tool"
    assert log.log_data["attempt"] == 1
    assert log.log_data["inputs"] == {"msg": "hello"}
    assert log.log_data["outputs"] == {"msg": "hello"}
    assert isinstance(log.log_data["latency_ms"], int)


async def test_flaky_tool_retries_then_succeeds(db_engine: AsyncEngine, tenant_and_agent) -> None:
    tenant_id, agent_id = tenant_and_agent

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="flaky_tool",
            input_data={"fail_count": 2},
            max_attempts=5,
        )
        await session.commit()
        task_id = task.id

    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    # Drive 3 worker iterations; between each we reset scheduled_for so the
    # backoff doesn't make the test sleep.
    for _ in range(3):
        handled = await run_once(worker_id="w-flaky", session_maker=session_maker)
        assert handled is True
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            await session.execute(
                update(Task).where(Task.id == task_id).values(scheduled_for=datetime.now(UTC))
            )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        refreshed = await session.get(Task, task_id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.succeeded
        assert refreshed.attempt == 3
        assert refreshed.output_data == {"succeeded_on_attempt": 3}

        logs = (
            (
                await session.execute(
                    select(AgentLog).where(AgentLog.task_id == task_id).order_by(AgentLog.logged_at)
                )
            )
            .scalars()
            .all()
        )

    # Two failed logs (attempts 1, 2) + one succeeded log (attempt 3).
    assert [log.action for log in logs] == [
        "tool.flaky_tool.failed",
        "tool.flaky_tool.failed",
        "tool.flaky_tool.succeeded",
    ]
    assert [log.severity for log in logs] == ["error", "error", "info"]
    assert [log.log_data["attempt"] for log in logs] == [1, 2, 3]
    for failed in logs[:2]:
        assert "flaky_tool: failing attempt" in failed.log_data["error"]
    assert logs[-1].log_data["outputs"] == {"succeeded_on_attempt": 3}


async def test_flaky_tool_failed_log_survives_handler_rollback(
    db_engine: AsyncEngine, tenant_and_agent
) -> None:
    """Explicit guard against the rollback trap: the failure agent_log must be
    visible from a fresh session even though the worker's tx rolled back.
    """
    tenant_id, agent_id = tenant_and_agent

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="flaky_tool",
            input_data={"fail_count": 1},
            max_attempts=1,  # one shot, then permanently failed
        )
        await session.commit()
        task_id = task.id

    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    await run_once(worker_id="w-rollback", session_maker=session_maker)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        refreshed = await session.get(Task, task_id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.failed
        log = (
            (await session.execute(select(AgentLog).where(AgentLog.task_id == task_id)))
            .scalars()
            .one()
        )
        assert log.action == "tool.flaky_tool.failed"
        assert log.log_data["attempt"] == 1
