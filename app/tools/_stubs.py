"""Stub tools used by W8 acceptance tests + as placeholders until real tools land.

Both are stateless: no module-level mutable state, no DB writes from `call()`.
FlakyTool's "fails N times" behaviour is driven by the `__attempt__` value the
handler threads in from `task.attempt`, so the orchestrator's retry budget
naturally exercises it.
"""

from typing import Any, ClassVar

from app.tools.base import Tool


class EchoTool(Tool):
    name: ClassVar[str] = "echo_tool"
    description: ClassVar[str] = "Returns the input map unchanged."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": True,
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": True,
    }

    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        # Strip the handler-injected attempt marker so the echo is clean.
        return {k: v for k, v in inputs.items() if k != "__attempt__"}


class FlakyTool(Tool):
    """Fails the first `fail_count` attempts (default 2), then succeeds.

    Determinism comes from the `__attempt__` value the handler injects (== the
    task's `attempt` field, which the orchestrator increments on every claim).
    """

    name: ClassVar[str] = "flaky_tool"
    description: ClassVar[str] = (
        "Fails N times then succeeds. Exercises the orchestrator retry path."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "fail_count": {
                "type": "integer",
                "minimum": 0,
                "description": "Number of attempts that should fail before success.",
            },
        },
        "additionalProperties": True,
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "succeeded_on_attempt": {"type": "integer"},
        },
    }

    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        attempt = int(inputs.get("__attempt__", 1))
        fail_count = int(inputs.get("fail_count", 2))
        if attempt <= fail_count:
            raise RuntimeError(f"flaky_tool: failing attempt {attempt} of expected {fail_count}")
        return {"succeeded_on_attempt": attempt}
