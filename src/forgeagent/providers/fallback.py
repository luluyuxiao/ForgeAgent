"""Ordered provider routing with no replay after visible streaming begins."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..contracts import ChatProvider, Message, ModelReply, TokenCallback, Usage


class PartialResponseError(RuntimeError):
    """Signals that switching providers would duplicate an already-started response."""


@dataclass(frozen=True)
class FallbackAttempt:
    model: str
    error: str


class FallbackProvider:
    def __init__(self, providers: list[ChatProvider]):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = tuple(providers)
        self._active = 0
        self._lock = threading.RLock()
        self.attempts: list[FallbackAttempt] = []

    @property
    def model(self) -> str:
        return self.providers[self._active].model

    @property
    def total_usage(self) -> Usage:
        return Usage(
            sum(item.total_usage.prompt_tokens for item in self.providers),
            sum(item.total_usage.completion_tokens for item in self.providers),
        )

    @total_usage.setter
    def total_usage(self, value: Usage) -> None:
        # Restored usage belongs to the active route; credentials/routes stay runtime-local.
        for provider in self.providers:
            provider.total_usage = Usage()
        self.providers[self._active].total_usage = value

    def complete(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        on_text: TokenCallback | None = None,
    ) -> ModelReply:
        with self._lock:
            order = list(range(self._active, len(self.providers))) + list(range(self._active))
            last: Exception | None = None
            for index in order:
                emitted = False

                def guarded_text(text: str) -> None:
                    nonlocal emitted
                    emitted = True
                    if on_text is not None:
                        on_text(text)

                try:
                    reply = self.providers[index].complete(messages, tools, on_text=guarded_text)
                    self._active = index
                    return reply
                except PartialResponseError:
                    raise
                except Exception as error:  # provider boundary intentionally catches all failures
                    if emitted:
                        raise PartialResponseError(
                            f"{self.providers[index].model} failed after streaming began"
                        ) from error
                    self.attempts.append(
                        FallbackAttempt(self.providers[index].model, f"{type(error).__name__}: {error}")
                    )
                    last = error
            assert last is not None
            raise RuntimeError("all provider routes failed") from last
