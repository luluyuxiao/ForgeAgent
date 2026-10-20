"""Tool registration, lazy schema exposure, and safe direct execution."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterable
from typing import Any, ClassVar

from ..contracts import ToolCall
from .base import Tool, ToolEffect

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

    def unregister(self, name: str) -> Tool | None:
        return self._tools.pop(name, None)

    def replace(self, tool: Tool) -> None:
        if not _TOOL_NAME.fullmatch(tool.name):
            raise ValueError(f"invalid Tool name: {tool.name!r}")
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


class ToolCatalog:
    def __init__(self, registry: ToolRegistry, *, active: set[str] | None = None):
        self.registry = registry
        self._active = set(registry.names() if active is None else active)

    def activate(self, names: Iterable[str]) -> list[str]:
        activated = []
        for name in names:
            if self.registry.get(name) is None:
                continue
            if name not in self._active:
                self._active.add(name)
                activated.append(name)
        return activated

    def hide(self, names: Iterable[str]) -> None:
        self._active.difference_update(names)

    def is_active(self, name: str) -> bool:
        return name in self._active

    def names(self) -> tuple[str, ...]:
        return tuple(name for name in self.registry.names() if name in self._active)

    def schemas(self) -> list[dict[str, Any]]:
        return [self.registry.require(name).schema() for name in self.names()]

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        needle = query.casefold()
        rows = []
        for name in self.registry.names():
            tool = self.registry.require(name)
            haystack = f"{name} {tool.description}".casefold()
            if needle in haystack:
                rows.append(
                    {
                        "name": name,
                        "description": tool.description,
                        "active": name in self._active,
                        "effect": tool.effect.value,
                    }
                )
            if len(rows) >= limit:
                break
        return rows


class SearchToolsTool(Tool):
    name = "search_tools"
    description = "Search the Tool Catalog for capabilities that are not currently loaded."
    effect = ToolEffect.PURE
    capabilities = frozenset()
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, catalog: ToolCatalog):
        self.catalog = catalog

    def execute(self, query: str, limit: int = 20) -> str:
        return json.dumps(self.catalog.search(query, limit), ensure_ascii=False, indent=2)


class ActivateToolsTool(Tool):
    name = "activate_tools"
    description = "Expose named catalog Tools to the model for later rounds."
    effect = ToolEffect.PURE
    capabilities = frozenset()
    parallel_safe = False
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["names"],
        "additionalProperties": False,
    }

    def __init__(self, catalog: ToolCatalog):
        self.catalog = catalog

    def execute(self, names: list[str]) -> str:
        activated = self.catalog.activate(names)
        unknown = [name for name in names if self.catalog.registry.get(name) is None]
        return json.dumps({"activated": activated, "unknown": unknown}, ensure_ascii=False)


def install_catalog_tools(catalog: ToolCatalog) -> None:
    for tool in (SearchToolsTool(catalog), ActivateToolsTool(catalog)):
        if catalog.registry.get(tool.name) is None:
            catalog.registry.register(tool)
        catalog.activate([tool.name])
