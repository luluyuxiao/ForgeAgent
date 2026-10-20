"""Lazy Tool schema exposure for large local and MCP Tool collections."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from .registry import ToolRegistry
from .tools import Tool, ToolEffect


class ToolCatalog:
    def __init__(self, registry: ToolRegistry, *, active: set[str] | None = None):
        self.registry = registry
        self._active = set(registry.names() if active is None else active)

    def activate(self, names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
        activated = []
        for name in names:
            if self.registry.get(name) is None:
                continue
            if name not in self._active:
                self._active.add(name)
                activated.append(name)
        return activated

    def hide(self, names: list[str] | tuple[str, ...] | set[str]) -> None:
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
