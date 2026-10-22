"""Bounded, local retrieval over the durable conversation Transcript."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from ..contracts import Message
from ..retrieval import bm25_scores, tokenize
from .base import Access, ResourceClaim, Tool, ToolEffect

TranscriptSource = Callable[[], Sequence[Message]]

_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_RETRIEVAL_TOOLS = frozenset({"search_transcript", "read_transcript_range"})
_MAX_QUERY_CHARS = 500
_MAX_INDEXED_MESSAGE_CHARS = 64_000
_MAX_SNIPPET_CHARS = 1_200
_MAX_RANGE_MESSAGE_CHARS = 2_500


class SearchTranscriptTool(Tool):
    """Find relevant earlier messages without re-injecting the full history."""

    name = "search_transcript"
    description = (
        "Search earlier conversation history with local BM25 when a relevant detail "
        "may have been removed from the active context. Returns original message indexes "
        "and bounded snippets; use read_transcript_range for exact nearby messages."
    )
    effect = ToolEffect.READ
    capabilities = frozenset({"conversation_read"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": _MAX_QUERY_CHARS},
            "roles": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_ALLOWED_ROLES)},
                "uniqueItems": True,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, source: TranscriptSource):
        self.source = source

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("conversation:transcript", Access.READ),)

    def execute(
        self,
        query: str,
        roles: list[str] | None = None,
        limit: int = 5,
    ) -> str:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > _MAX_QUERY_CHARS:
            raise ValueError(f"query must be at most {_MAX_QUERY_CHARS} characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        selected_roles = _validate_roles(roles)
        messages, history_count = _earlier_messages(self.source())
        documents = {
            str(index): _message_document(message)
            for index, message in enumerate(messages)
            if not selected_roles or message.get("role") in selected_roles
        }
        scores = bm25_scores(query, documents)
        matches = sorted(
            ((int(index), score) for index, score in scores.items()),
            key=lambda item: (-item[1], -item[0]),
        )[:limit]
        results = []
        for index, score in matches:
            message = messages[index]
            results.append(
                {
                    "index": index,
                    "role": str(message.get("role", "unknown")),
                    "name": message.get("name"),
                    "score": round(score, 6),
                    "snippet": _snippet(documents[str(index)], query),
                }
            )
        return json.dumps(
            {
                "query": query,
                "history_message_count": history_count,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )


class ReadTranscriptRangeTool(Tool):
    """Read a small exact range selected from search results."""

    name = "read_transcript_range"
    description = (
        "Read a bounded range of earlier conversation messages by original Transcript "
        "index. Use after search_transcript to inspect exact nearby context."
    )
    effect = ToolEffect.READ
    capabilities = frozenset({"conversation_read"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "start": {"type": "integer", "minimum": 0},
            "count": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
        },
        "required": ["start"],
        "additionalProperties": False,
    }

    def __init__(self, source: TranscriptSource):
        self.source = source

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("conversation:transcript", Access.READ),)

    def execute(self, start: int, count: int = 5) -> str:
        if start < 0:
            raise ValueError("start must be non-negative")
        if not 1 <= count <= 8:
            raise ValueError("count must be between 1 and 8")
        messages, history_count = _earlier_messages(self.source())
        if start >= history_count:
            rows: list[dict[str, Any]] = []
        else:
            end = min(start + count, history_count)
            rows = [
                _project_message(index, messages[index]) for index in range(start, end)
            ]
        return json.dumps(
            {
                "start": start,
                "requested_count": count,
                "history_message_count": history_count,
                "messages": rows,
            },
            ensure_ascii=False,
            indent=2,
        )


def _validate_roles(roles: list[str] | None) -> frozenset[str]:
    if roles is None:
        return frozenset()
    selected = frozenset(roles)
    invalid = sorted(selected - _ALLOWED_ROLES)
    if invalid:
        raise ValueError(f"unknown roles: {', '.join(invalid)}")
    return selected


def _earlier_messages(messages: Sequence[Message]) -> tuple[list[Message], int]:
    """Exclude the live user turn while either retrieval Tool is executing."""
    copied = list(messages)
    cutoff = len(copied)
    if copied and _contains_retrieval_call(copied[-1]):
        for index in range(len(copied) - 2, -1, -1):
            if copied[index].get("role") == "user":
                cutoff = index
                break
    return copied[:cutoff], cutoff


def _contains_retrieval_call(message: Message) -> bool:
    if message.get("role") != "assistant":
        return False
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return False
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict) and function.get("name") in _RETRIEVAL_TOOLS:
            return True
    return False


def _message_document(message: Message) -> str:
    parts = [f"role: {message.get('role', 'unknown')}"]
    if message.get("name") is not None:
        parts.append(f"name: {_as_text(message['name'])}")
    if message.get("content") is not None:
        parts.append(_as_text(message["content"]))
    if message.get("tool_calls") is not None:
        parts.append(f"tool_calls: {_as_text(message['tool_calls'])}")
    return _clip("\n".join(parts), _MAX_INDEXED_MESSAGE_CHARS)


def _project_message(index: int, message: Message) -> dict[str, Any]:
    copied = copy.deepcopy(message)
    rendered = json.dumps(copied, ensure_ascii=False, default=str)
    if len(rendered) <= _MAX_RANGE_MESSAGE_CHARS:
        return {"index": index, **copied}
    projected: dict[str, Any] = {
        "index": index,
        "role": str(message.get("role", "unknown")),
        "content": _clip(_message_document(message), _MAX_RANGE_MESSAGE_CHARS),
        "truncated": True,
    }
    if message.get("name") is not None:
        projected["name"] = _as_text(message["name"])
    if message.get("tool_call_id") is not None:
        projected["tool_call_id"] = _as_text(message["tool_call_id"])
    return projected


def _snippet(document: str, query: str) -> str:
    if len(document) <= _MAX_SNIPPET_CHARS:
        return document
    folded = document.casefold()
    position = folded.find(query.casefold())
    if position < 0:
        for token in tokenize(query):
            position = folded.find(token)
            if position >= 0:
                break
    position = max(position, 0)
    start = max(0, position - _MAX_SNIPPET_CHARS // 3)
    end = min(len(document), start + _MAX_SNIPPET_CHARS)
    start = max(0, end - _MAX_SNIPPET_CHARS)
    prefix = "…" if start else ""
    suffix = "…" if end < len(document) else ""
    return prefix + document[start:end] + suffix


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "…[truncated]"
    return value[: limit - len(marker)] + marker
