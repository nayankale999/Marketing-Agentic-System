"""Worker loop: claim a task, dispatch to its handler, complete or fail.

Run as a process via `python -m app.orchestrator.worker` (see `make worker`).
For tests, call `run_once(...)` directly.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal
from app.orchestrator.handlers import register_builtin_handlers
from app.orchestrator.queue import claim_next, complete, fail, reap_expired_leases
from app.orchestrator.registry import get_handler

log = logging.getLogger(__name__)


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def run_once(
    *,
    worker_id: str,
    session_maker: Callable[[], AsyncSession] = SessionLocal,
    lease_seconds: int = 300,
) -> bool:
    """Process up to one task. Returns True if a task was handled, False if
    the queue was empty.
    """
    # 1) Claim in its own tx so other workers see it as running.
    async with session_maker() as session, session.begin():
        task = await claim_next(session, worker_id=worker_id, lease_seconds=lease_seconds)
    if task is None:
        return False

    # 2) Dispatch to the handler. Handler runs in its own session/tx.
    handler = get_handler(task.skill_name)
    if handler is None:
        async with session_maker() as session, session.begin():
            await fail(
                session,
                task.id,
                error=f"no handler registered for skill '{task.skill_name}'",
                permanent=True,
            )
        log.warning("task %s failed: no handler for skill %s", task.id, task.skill_name)
        return True

    try:
        async with session_maker() as session, session.begin():
            output = await handler(session, task)
        async with session_maker() as session, session.begin():
            await complete(session, task.id, output_data=output or {})
        log.info("task %s succeeded (skill=%s)", task.id, task.skill_name)
    except Exception as exc:
        async with session_maker() as session, session.begin():
            await fail(session, task.id, error=str(exc))
        log.exception("task %s failed (skill=%s)", task.id, task.skill_name)

    return True


async def run_loop(
    *,
    worker_id: str | None = None,
    poll_interval_seconds: float = 1.0,
    reap_interval_seconds: float = 30.0,
    session_maker: Callable[[], AsyncSession] = SessionLocal,
) -> None:
    """Run until cancelled. Reaps expired leases on its own cadence."""
    wid = worker_id or _default_worker_id()
    log.info("worker %s starting", wid)
    reap_task: asyncio.Task[None] | None = asyncio.create_task(
        _reap_loop(reap_interval_seconds, session_maker)
    )
    try:
        while True:
            handled = await run_once(worker_id=wid, session_maker=session_maker)
            if not handled:
                await asyncio.sleep(poll_interval_seconds)
    finally:
        if reap_task is not None:
            reap_task.cancel()


async def _reap_loop(
    interval_seconds: float,
    session_maker: Callable[[], AsyncSession],
) -> None:
    while True:
        try:
            async with session_maker() as session, session.begin():
                reaped = await reap_expired_leases(session)
            if reaped:
                log.info("reaped %d expired lease(s)", reaped)
        except Exception:
            log.exception("reaper iteration failed")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    register_builtin_handlers()
    asyncio.run(run_loop())


if __name__ == "__main__":
    main()
