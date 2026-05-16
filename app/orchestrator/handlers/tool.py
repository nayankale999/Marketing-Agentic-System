"""Adapt a `Tool` into a queue handler.

Each registered tool gets a handler under skill_name == tool.name. The handler
threads `task.attempt` through as `__attempt__` (so tools like FlakyTool can be
deterministic across retries), times the call, and writes an `agent_log` row
recording the outcome — committed BEFORE re-raising on failure so the worker's
tx rollback doesn't lose it.
"""

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.log import agent_log_emit
from app.db.models import Task
from app.orchestrator.registry import HandlerFn, register_handler
from app.tools.base import Tool, tool_registry


def _build_tool_handler(tool: Tool) -> HandlerFn:
    async def _handler(session: AsyncSession, task: Task) -> dict[str, Any]:
        inputs: dict[str, Any] = {**task.input_data, "__attempt__": task.attempt}
        start = time.monotonic()
        try:
            outputs = await tool.call(inputs)
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            await agent_log_emit(
                session,
                tenant_id=task.tenant_id,
                agent_id=task.agent_id,
                task_id=task.id,
                action=f"tool.{tool.name}.failed",
                log_data={
                    "tool": tool.name,
                    "latency_ms": latency_ms,
                    "attempt": task.attempt,
                    "error": str(exc),
                },
                severity="error",
            )
            # Commit the log row before the worker's `async with session.begin():`
            # rolls back this tx on the propagated exception.
            await session.commit()
            raise

        latency_ms = int((time.monotonic() - start) * 1000)
        await agent_log_emit(
            session,
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            task_id=task.id,
            action=f"tool.{tool.name}.succeeded",
            log_data={
                "tool": tool.name,
                "latency_ms": latency_ms,
                "attempt": task.attempt,
                "inputs": {k: v for k, v in inputs.items() if k != "__attempt__"},
                "outputs": outputs,
            },
        )
        return outputs

    return _handler


def register_tool_handlers() -> None:
    """Register a handler for every tool currently in the registry."""
    for tool in tool_registry.all():
        register_handler(tool.name, _build_tool_handler(tool))
