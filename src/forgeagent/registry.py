"""Instance-scoped Tool registry and safe execution wrapper."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable
from typing import Any

from .contracts import ToolCall
from .tools import Tool

_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_RESULT_LIMIT = 32_000


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not _TOOL_NAME.fullmatch(tool.name):
            raise ValueError(f"invalid Tool name: {tool.name!r}")
        if tool.name in self._tools:
            raise ValueError(f"duplicate Tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(name)
        return tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def execute(self, call: ToolCall) -> str:
        if call.parse_error:
            return f"Error: {call.parse_error}"
        tool = self.get(call.name)
        if tool is None:
            return f"Error: unknown Tool {call.name!r}"
        try:
            inspect.signature(tool.execute).bind(**call.arguments)
        except TypeError as error:
            return f"Error: invalid arguments for {call.name}: {error}"
        try:
            result = tool.execute(**call.arguments)
        except Exception as error:  # noqa: BLE001 - Tool failures are model observations
            return f"Error executing {call.name}: {type(error).__name__}: {error}"
        rendered = result if isinstance(result, str) else str(result)
        if len(rendered) > _RESULT_LIMIT:
            omitted = len(rendered) - _RESULT_LIMIT
            return rendered[:_RESULT_LIMIT] + f"\n[truncated {omitted} characters]"
        return rendered

    def __len__(self) -> int:
        return len(self._tools)
