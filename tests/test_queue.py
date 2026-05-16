"""W6: durable task queue acceptance tests.

Covers: enqueue/claim/complete round trip, idempotency on duplicate key,
crash recovery via lease expiry, retry/backoff on handler failure, retry
exhaustion -> failed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db.enums import AgentKind, TaskStatus
from app.db.models import Agent, Task, Tenant
from app.orchestrator.handlers import register_builtin_handlers
from app.orchestrator.queue import (
    claim_next,
    enqueue_task,
    fail,
    reap_expired_leases,
)
from app.orchestrator.registry import register_handler
from app.orchestrator.worker import run_once

# Make sure the echo handler is registered for these tests.
register_builtin_handlers()


@pytest.fixture(autouse=True)
async def _clean_task_queue(db_engine: AsyncEngine):
    """Reset the task table before each test in this module.

    `claim_next` has no tenant filter (workers pick up any ready task), so
    tasks from prior tests would leak into later assertions about "the next
    claim" / "empty queue".
    """
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await session.execute(text("DELETE FROM task"))
    yield


@pytest.fixture
async def make_tenant_and_agent(db_engine: AsyncEngine):
    """Create one tenant + one agent; return their ids."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tenant = Tenant(name=f"queue-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        agent = Agent(
            tenant_id=tenant.id,
            name=f"echo-{uuid.uuid4().hex[:8]}",
            agent_type=AgentKind.orchestrator,
        )
        session.add(agent)
        await session.commit()
        return tenant.id, agent.id


async def test_enqueue_claim_complete(db_engine: AsyncEngine, make_tenant_and_agent) -> None:
    tenant_id, agent_id = make_tenant_and_agent

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="echo",
            input_data={"msg": "hello"},
        )
        await session.commit()
        task_id = task.id

    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    handled = await run_once(worker_id="test-worker", session_maker=session_maker)
    assert handled is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        refreshed = await session.get(Task, task_id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.succeeded
        assert refreshed.output_data == {"msg": "hello"}
        assert refreshed.attempt == 1
        assert refreshed.completed_at is not None
        assert refreshed.leased_until is None
        assert refreshed.worker_id is None


async def test_idempotent_enqueue_returns_existing(
    db_engine: AsyncEngine, make_tenant_and_agent
) -> None:
    tenant_id, agent_id = make_tenant_and_agent
    key = f"idem-{uuid.uuid4()}"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        first = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="echo",
            input_data={"v": 1},
            idempotency_key=key,
        )
        second = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="echo",
            input_data={"v": 2},
            idempotency_key=key,
        )
        await session.commit()

    assert first.id == second.id
    assert first.input_data == {"v": 1}, "first write wins on idempotency conflict"


async def test_crash_recovery_via_lease_expiry(
    db_engine: AsyncEngine, make_tenant_and_agent
) -> None:
    tenant_id, agent_id = make_tenant_and_agent

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="echo",
            input_data={"step": "first"},
        )
        await session.commit()
        task_id = task.id

    # Worker A claims, then "crashes" before completing.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        claimed = await claim_next(session, worker_id="worker-a", lease_seconds=300)
    assert claimed is not None
    assert claimed.id == task_id
    assert claimed.status == TaskStatus.running
    assert claimed.attempt == 1

    # Simulate lease expiry (worker A vanished).
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(leased_until=datetime.now(UTC) - timedelta(seconds=1))
        )

    # Reaper recovers the task.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        reaped = await reap_expired_leases(session)
    assert reaped == 1

    # Worker B claims successfully.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        reclaimed = await claim_next(session, worker_id="worker-b", lease_seconds=300)
    assert reclaimed is not None
    assert reclaimed.id == task_id
    assert reclaimed.attempt == 2
    assert reclaimed.worker_id == "worker-b"


async def test_retry_with_backoff_then_exhaustion(
    db_engine: AsyncEngine, make_tenant_and_agent
) -> None:
    tenant_id, agent_id = make_tenant_and_agent

    # Register a handler that always raises.
    async def boom(session: AsyncSession, task: Task) -> dict:
        raise RuntimeError("boom")

    register_handler("boom", boom)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="boom",
            input_data={},
            max_attempts=2,
        )
        await session.commit()
        task_id = task.id

    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(UTC)

    # Attempt 1 fails -> awaiting_retry with backoff.
    handled = await run_once(worker_id="w", session_maker=session_maker)
    assert handled is True
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        t = await session.get(Task, task_id)
        assert t is not None
        assert t.status == TaskStatus.awaiting_retry
        assert t.attempt == 1
        assert t.scheduled_for > now, "backoff pushed scheduled_for into the future"
        assert "boom" in (t.error_message or "")
        # Force the next attempt to be eligible immediately.
        await session.execute(
            update(Task).where(Task.id == task_id).values(scheduled_for=datetime.now(UTC))
        )
        await session.commit()

    # Attempt 2 fails -> max_attempts reached -> status=failed.
    handled = await run_once(worker_id="w", session_maker=session_maker)
    assert handled is True
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        t = await session.get(Task, task_id)
        assert t is not None
        assert t.status == TaskStatus.failed
        assert t.attempt == 2
        assert t.completed_at is not None


async def test_no_handler_registered_fails_permanently(
    db_engine: AsyncEngine, make_tenant_and_agent
) -> None:
    tenant_id, agent_id = make_tenant_and_agent
    skill = f"missing-{uuid.uuid4().hex[:8]}"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name=skill,
            input_data={},
        )
        await session.commit()
        task_id = task.id

    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    await run_once(worker_id="w", session_maker=session_maker)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        t = await session.get(Task, task_id)
        assert t is not None
        assert t.status == TaskStatus.failed
        assert "no handler" in (t.error_message or "")


async def test_claim_returns_none_when_queue_empty(db_engine: AsyncEngine) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        claimed = await claim_next(session, worker_id="w-empty", lease_seconds=300)
    assert claimed is None


async def test_fail_permanent_skips_retry(db_engine: AsyncEngine, make_tenant_and_agent) -> None:
    tenant_id, agent_id = make_tenant_and_agent

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_name="echo",
            input_data={},
            max_attempts=5,
        )
        await session.commit()
        task_id = task.id

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        claimed = await claim_next(session, worker_id="w", lease_seconds=60)
    assert claimed is not None

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await fail(session, task_id, error="invalid input", permanent=True)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        t = await session.get(Task, task_id)
        assert t is not None
        assert t.status == TaskStatus.failed
        assert t.attempt == 1, "permanent failure does not consume the retry budget further"
