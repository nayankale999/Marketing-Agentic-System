"""Tool ABC + registry.

Tools are skill primitives the agents can call (SEO, copywriting, segmentation,
A/B testing, social, email — see `docs/backlog/stories/E11-skills-tools.md`).
Each Tool has a name, description, and JSON Schemas for inputs/outputs that
match what the Claude Agent SDK will register when an agent is run.

For W8 we provide the base class + registry + two stub tools. Real tools
(seo.analysis, copywriting.generate, ...) land as their epic-level work
units arrive.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class Tool(ABC):
    """A registered skill primitive.

    Subclasses declare `name`, `description`, `input_schema`, `output_schema`
    as class attributes and implement `call(inputs)`.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]
    output_schema: ClassVar[dict[str, Any]]

    @abstractmethod
    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Execute the tool. Return JSON-serialisable outputs or raise."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools.keys())


tool_registry = ToolRegistry()
