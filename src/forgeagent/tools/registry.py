"""Tool registration, lazy schema exposure, and safe direct execution."""

from __future__ import annotations

import inspect
import json
import math
import re
import threading
from collections.abc import Iterable
from typing import Any, ClassVar, Protocol

from ..contracts import ToolCall
from ..retrieval import bm25_scores
from .base import Tool, ToolEffect

_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_RESULT_LIMIT = 32_000
_PLANNING_EFFECTS = frozenset({ToolEffect.PURE, ToolEffect.READ})
_NONINTERACTIVE_EFFECTS = frozenset(
    {ToolEffect.PURE, ToolEffect.READ, ToolEffect.STATE}
)


class EmbeddingBackend(Protocol):
    """Optional dense-vector backend used by Tool Catalog search."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


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
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        active: set[str] | None = None,
        embedder: EmbeddingBackend | None = None,
        allowed_capabilities: frozenset[str] | None = None,
        interactive: bool = True,
        allow_all: bool = False,
    ):
        self.registry = registry
        self._active = set(registry.names() if active is None else active)
        self.embedder = embedder
        self.allowed_capabilities = allowed_capabilities
        self.interactive = interactive
        self.allow_all = allow_all
        self._planning = False
        self._embedding_cache: dict[str, tuple[str, list[float]]] = {}
        self._cache_lock = threading.RLock()

    @property
    def uses_embeddings(self) -> bool:
        return self.embedder is not None

    def set_planning(self, enabled: bool) -> None:
        self._planning = enabled

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
        if limit < 1:
            return []
        query = query.strip()
        if not query:
            return []

        tools = [
            self.registry.require(name)
            for name in self.registry.names()
            if self.denial_reason(self.registry.require(name)) is None
        ]
        if not tools:
            return []

        documents = {tool.name: _search_document(tool) for tool in tools}
        lexical = bm25_scores(query, documents)
        semantic: dict[str, float] = {}
        semantic_status = "disabled"
        if self.embedder is not None:
            try:
                semantic = self._semantic_scores(query, documents)
            except Exception:  # noqa: BLE001 - retrieval falls back to offline BM25
                semantic_status = "fallback"
            else:
                semantic_status = "used"

        candidates = set(lexical) | set(semantic)
        if not candidates:
            return []
        max_lexical = max(lexical.values(), default=0.0)
        lexical_weight = 0.55 if semantic else 1.0
        semantic_weight = 0.45 if semantic else 0.0
        rows = []
        for name in candidates:
            tool = self.registry.require(name)
            bm25 = lexical.get(name, 0.0)
            cosine = semantic.get(name)
            lexical_score = bm25 / max_lexical if max_lexical > 0 else 0.0
            similarity = 0.0 if cosine is None else cosine
            semantic_score = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
            score = lexical_weight * lexical_score + semantic_weight * semantic_score
            matched_by = []
            if bm25 > 0:
                matched_by.append("bm25")
            if cosine is not None:
                matched_by.append("embedding")
            rows.append(
                {
                    "name": name,
                    "description": tool.description,
                    "active": name in self._active,
                    "effect": tool.effect.value,
                    "capabilities": sorted(tool.capabilities),
                    "requires_permission": tool.effect not in _NONINTERACTIVE_EFFECTS,
                    "score": round(score, 6),
                    "matched_by": matched_by,
                    "semantic_status": semantic_status,
                }
            )
        rows.sort(key=lambda row: (-float(row["score"]), str(row["name"])))
        return rows[: min(limit, 50)]

    def denial_reason(self, tool: Tool) -> str | None:
        if self.allowed_capabilities is not None:
            missing = sorted(tool.capabilities - self.allowed_capabilities)
            if missing:
                return f"missing capabilities: {', '.join(missing)}"
        if self._planning and tool.effect not in _PLANNING_EFFECTS:
            return f"effect {tool.effect.value} is unavailable in Plan Mode"
        if (
            not self.interactive
            and not self.allow_all
            and tool.effect not in _NONINTERACTIVE_EFFECTS
        ):
            return f"effect {tool.effect.value} requires an interactive approver or --yes"
        return None

    def activation_denials(self, names: Iterable[str]) -> dict[str, str]:
        denied = {}
        for name in names:
            tool = self.registry.get(name)
            if tool is None:
                continue
            reason = self.denial_reason(tool)
            if reason is not None:
                denied[name] = reason
        return denied

    def _semantic_scores(
        self, query: str, documents: dict[str, str]
    ) -> dict[str, float]:
        assert self.embedder is not None
        cached = {}
        with self._cache_lock:
            for name, text in documents.items():
                entry = self._embedding_cache.get(name)
                if entry is not None and entry[0] == text:
                    cached[name] = entry[1]
        missing = [name for name in documents if name not in cached]
        vectors = self.embedder.embed([query, *(documents[name] for name in missing)])
        if len(vectors) != len(missing) + 1:
            raise ValueError("embedding backend returned an unexpected vector count")
        query_vector = _validated_vector(vectors[0])
        new_cache = {}
        for name, raw_vector in zip(missing, vectors[1:], strict=True):
            vector = _validated_vector(raw_vector)
            cached[name] = vector
            new_cache[name] = (documents[name], vector)
        with self._cache_lock:
            self._embedding_cache.update(new_cache)
        return {
            name: _cosine_similarity(query_vector, vector)
            for name, vector in cached.items()
            if len(vector) == len(query_vector)
        }


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
        if catalog.uses_embeddings:
            self.effect = ToolEffect.NETWORK
            self.capabilities = frozenset({"network"})
            self.parallel_safe = False

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
        denied = self.catalog.activation_denials(names)
        activated = self.catalog.activate(name for name in names if name not in denied)
        unknown = [name for name in names if self.catalog.registry.get(name) is None]
        return json.dumps(
            {"activated": activated, "denied": denied, "unknown": unknown},
            ensure_ascii=False,
        )


def install_catalog_tools(catalog: ToolCatalog) -> None:
    for tool in (SearchToolsTool(catalog), ActivateToolsTool(catalog)):
        if catalog.registry.get(tool.name) is None:
            catalog.registry.register(tool)
        catalog.activate([tool.name])


def _search_document(tool: Tool) -> str:
    name = tool.name.replace("__", " ").replace("_", " ")
    capabilities = " ".join(sorted(tool.capabilities))
    return f"{name} {name} {name} {tool.description} {capabilities} {tool.effect.value}"

def _validated_vector(vector: list[float]) -> list[float]:
    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("embedding backend returned an invalid vector")
    return values


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding vectors use inconsistent dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
