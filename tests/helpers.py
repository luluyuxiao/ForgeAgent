from __future__ import annotations

import copy
from collections import deque

from forgeagent.contracts import Message, ModelReply, TokenCallback, Usage


class ScriptedProvider:
    def __init__(self, replies: list[ModelReply], model: str = "scripted"):
        self.replies = deque(replies)
        self.model = model
        self.total_usage = Usage()
        self.requests: list[tuple[list[Message], list[dict]]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        on_text: TokenCallback | None = None,
    ) -> ModelReply:
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        if not self.replies:
            raise AssertionError("ScriptedProvider has no reply left")
        reply = self.replies.popleft()
        if on_text is not None and reply.content:
            on_text(reply.content)
        self.total_usage = Usage(
            self.total_usage.prompt_tokens + reply.usage.prompt_tokens,
            self.total_usage.completion_tokens + reply.usage.completion_tokens,
        )
        return reply

