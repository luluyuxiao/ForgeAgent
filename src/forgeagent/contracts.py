"""Shared data contracts at the boundaries of the runtime."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

Message = dict[str, Any]
TokenCallback = Callable[[str], None]
EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None

    def as_openai_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ModelReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def as_message(self) -> Message:
        message: Message = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [call.as_openai_dict() for call in self.tool_calls]
        return message


class ChatProvider(Protocol):
    """The only model-facing interface required by AgentRuntime."""

    model: str
    total_usage: Usage

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        on_text: TokenCallback | None = None,
    ) -> ModelReply:
        ...

