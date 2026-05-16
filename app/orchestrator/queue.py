"""Durable task queue backed by the `task` table.

Pattern:
  - enqueue_task() upserts on (tenant_id, idempotency_key) so retries from the
    caller never produce duplicates.
  - claim_next() uses SELECT ... FOR UPDATE SKIP LOCKED to atomically pull the
    next ready task, set status=running, increment attempt, stamp leased_until,
    and return the row. The caller commits.
  - complete() / fail() finalise the task. fail() flips to awaiting_retry with
    exponential backoff + jitter until max_attempts is hit, then to failed.
  - reap_expired_leases() recovers tasks whose worker crashed mid-execution
    (status=running and leased_until in the past) back to awaiting_retry for
    another worker to pick up.
"""

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import TaskStatus
from app.db.models import Task

# Exponential backoff parameters for retries.
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_MAX_SECONDS = 3600.0
_BACKOFF_JITTER_FRACTION = 0.1


def _now() -> datetime:
    return datetime.now(UTC)


def _compute_next_attempt_delay(attempt: int) -> timedelta:
    """Exponential backoff with 10% jitter, capped at _BACKOFF_MAX_SECONDS."""
    delay = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
    jitter = random.uniform(0, delay * _BACKOFF_JITTER_FRACTION)
    return timedelta(seconds=delay + jitter)


async def enqueue_task(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    skill_name: str,
    input_data: dict[str, Any] | None = None,
    campaign_id: UUID | None = None,
    parent_task_id: UUID | None = None,
    idempotency_key: str | None = None,
    scheduled_for: datetime | None = None,
    max_attempts: int = 3,
    priority: int = 5,
) -> Task:
    """Insert a task. If idempotency_key collides on (tenant_id, key), return
    the existing task instead of erroring.
    """
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "campaign_id": campaign_id,
        "parent_task_id": parent_task_id,
        "skill_name": skill_name,
        "input_data": input_data or {},
        "idempotency_key": idempotency_key,
        "max_attempts": max_attempts,
        "priority": priority,
        "status": TaskStatus.queued,
    }
    if scheduled_for is not None:
        values["scheduled_for"] = scheduled_for

    base_stmt = pg_insert(Task).values(**values)
    if idempotency_key is not None:
        # Match migration 0005's partial unique index predicate exactly.
        base_stmt = base_stmt.on_conflict_do_nothing(
            index_elements=["tenant_id", "idempotency_key"],
            index_where=text("idempotency_key IS NOT NULL"),
        )
    stmt = base_stmt.returning(Task.id)

    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    if inserted_id is None:
        # Conflict: idempotency_key already exists for this tenant.
        existing = (
            await session.execute(
                select(Task).where(
                    Task.tenant_id == tenant_id,
                    Task.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
        return existing

    task = await session.get(Task, inserted_id)
    assert task is not None
    return task


async def claim_next(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> Task | None:
    """Atomically claim the next ready task. Returns None if the queue is empty.

    Ready = status in (queued, awaiting_retry) AND scheduled_for <= now().
    Ordering: highest priority first, then earliest scheduled.
    """
    now = _now()
    leased_until = now + timedelta(seconds=lease_seconds)

    inner = (
        select(Task.id)
        .where(
            Task.status.in_([TaskStatus.queued, TaskStatus.awaiting_retry]),
            Task.scheduled_for <= now,
        )
        .order_by(Task.priority.desc(), Task.scheduled_for.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    stmt = (
        update(Task)
        .where(Task.id == inner.scalar_subquery())
        .values(
            status=TaskStatus.running,
            worker_id=worker_id,
            leased_until=leased_until,
            started_at=now,
            attempt=Task.attempt + 1,
            error_message=None,
        )
        .returning(Task)
    )

    row = (await session.execute(stmt)).scalar_one_or_none()
    return row


async def complete(
    session: AsyncSession,
    task_id: UUID,
    *,
    output_data: dict[str, Any] | None = None,
) -> None:
    """Mark a running task as succeeded and stash the output payload."""
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.succeeded,
            output_data=output_data or {},
            completed_at=_now(),
            leased_until=None,
            worker_id=None,
            error_message=None,
        )
    )


async def fail(
    session: AsyncSession,
    task_id: UUID,
    *,
    error: str,
    permanent: bool = False,
) -> Task:
    """Record a failure. Retries with exponential backoff up to max_attempts,
    then transitions to `failed`. Set `permanent=True` to skip retries (e.g.
    for non-retryable errors like invalid input).
    """
    task = await session.get(Task, task_id)
    assert task is not None, f"task {task_id} not found"

    if permanent or task.attempt >= task.max_attempts:
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=TaskStatus.failed,
                error_message=error,
                completed_at=_now(),
                leased_until=None,
                worker_id=None,
            )
        )
    else:
        delay = _compute_next_attempt_delay(task.attempt)
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=TaskStatus.awaiting_retry,
                error_message=error,
                scheduled_for=_now() + delay,
                leased_until=None,
                worker_id=None,
            )
        )

    await session.refresh(task)
    return task


async def reap_expired_leases(session: AsyncSession) -> int:
    """Recover crashed-worker tasks. Returns the number of rows reaped."""
    result = await session.execute(
        update(Task)
        .where(Task.status == TaskStatus.running, Task.leased_until < _now())
        .values(
            status=TaskStatus.awaiting_retry,
            leased_until=None,
            worker_id=None,
        )
    )
    # CursorResult.rowcount is set for UPDATE; mypy sees the base Result type.
    return result.rowcount or 0  # type: ignore[attr-defined]
