"""Streaming adapter for OpenAI-compatible Chat Completions providers."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from ..contracts import Message, ModelReply, TokenCallback, ToolCall, Usage


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_output_tokens: int = 8192,
        temperature: float = 0.0,
        retries: int = 3,
        retry_base_seconds: float = 0.5,
    ):
        if not model:
            raise ValueError("model must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.retries = max(0, retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.total_usage = Usage()

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        on_text: TokenCallback | None = None,
    ) -> ModelReply:
        include_usage = True
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            emitted = False
            try:
                stream = self._create_stream(messages, tools, include_usage=include_usage)
                reply, emitted = self._consume(stream, on_text)
                self.total_usage = Usage(
                    self.total_usage.prompt_tokens + reply.usage.prompt_tokens,
                    self.total_usage.completion_tokens + reply.usage.completion_tokens,
                )
                return reply
            except _StreamFailure as failure:
                error = failure.original
                last_error = error
                if (
                    isinstance(error, BadRequestError)
                    and include_usage
                    and not failure.emitted
                    and "stream" in str(error).lower()
                ):
                    include_usage = False
                    continue
                if failure.emitted or attempt >= self.retries or not _retryable(error):
                    raise error
                time.sleep(self.retry_base_seconds * (2**attempt))
            except BadRequestError as error:
                if include_usage and not emitted and "stream" in str(error).lower():
                    include_usage = False
                    last_error = error
                    continue
                raise
            except Exception as error:
                last_error = error
                if emitted or attempt >= self.retries or not _retryable(error):
                    raise
                time.sleep(self.retry_base_seconds * (2**attempt))

        assert last_error is not None
        raise last_error

    def _create_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        include_usage: bool,
    ):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}
        return self._client.chat.completions.create(**kwargs)

    def _consume(self, stream, on_text: TokenCallback | None) -> tuple[ModelReply, bool]:
        content: list[str] = []
        fragments: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"id": "", "name": "", "arguments": []}
        )
        usage = Usage()
        response_model = self.model
        emitted = False

        try:
            for chunk in stream:
                response_model = getattr(chunk, "model", None) or response_model
                raw_usage = getattr(chunk, "usage", None)
                if raw_usage is not None:
                    usage = Usage(
                        int(getattr(raw_usage, "prompt_tokens", 0) or 0),
                        int(getattr(raw_usage, "completion_tokens", 0) or 0),
                    )
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    emitted = True
                    content.append(text)
                    if on_text is not None:
                        on_text(text)
                for piece in getattr(delta, "tool_calls", None) or []:
                    emitted = True
                    index = int(getattr(piece, "index", 0) or 0)
                    target = fragments[index]
                    if getattr(piece, "id", None):
                        target["id"] = piece.id
                    function = getattr(piece, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            target["name"] += function.name
                        if getattr(function, "arguments", None):
                            target["arguments"].append(function.arguments)
        except Exception as error:
            raise _StreamFailure(error, emitted) from error

        calls: list[ToolCall] = []
        for index in sorted(fragments):
            item = fragments[index]
            raw_arguments = "".join(item["arguments"]) or "{}"
            parse_error = None
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise TypeError("arguments must decode to an object")
            except (json.JSONDecodeError, TypeError) as error:
                arguments = {}
                parse_error = f"invalid Tool Call arguments: {error}"
            calls.append(
                ToolCall(
                    id=item["id"] or f"call_{index}",
                    name=item["name"],
                    arguments=arguments,
                    parse_error=parse_error,
                )
            )

        return (
            ModelReply(
                content="".join(content),
                tool_calls=calls,
                usage=usage,
                model=response_model,
            ),
            emitted,
        )


def _retryable(error: Exception) -> bool:
    if isinstance(error, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    if isinstance(error, APIStatusError):
        return int(getattr(error, "status_code", 0) or 0) >= 500
    return False


class _StreamFailure(Exception):
    def __init__(self, original: Exception, emitted: bool):
        super().__init__(str(original))
        self.original = original
        self.emitted = emitted
