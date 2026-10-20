"""Whole-request token budgeting for compactable active context."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import ChatProvider, Message


class ContextLimitError(RuntimeError):
    pass


class Summarizer(Protocol):
    def summarize(self, messages: list[Message], target_tokens: int) -> str: ...


class ProviderSummarizer:
    """Use the configured model provider to compact older conversation history."""

    def __init__(self, provider: ChatProvider, lock: threading.RLock | None = None):
        self.provider = provider
        self.lock = lock or threading.RLock()

    def summarize(self, messages: list[Message], target_tokens: int) -> str:
        payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            reply = self.provider.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Summarize earlier coding-agent history. Preserve user requirements, "
                            "decisions, file paths, edits, tool failures, and unresolved work. "
                            "Do not invent facts. Return only the compact summary."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Target about {target_tokens} tokens. History:\n{payload}",
                    },
                ],
                [],
            )
        if not reply.content.strip():
            raise RuntimeError("summary provider returned empty content")
        return reply.content.strip()


@dataclass(frozen=True)
class CompactionReport:
    before_tokens: int
    after_tokens: int
    snipped_tool_results: int = 0
    collapsed_messages: int = 0
    summarized_messages: int = 0


@dataclass(frozen=True)
class PreparedContext:
    request_messages: list[Message]
    active_messages: list[Message]
    report: CompactionReport


class ContextWindow:
    def __init__(
        self,
        max_tokens: int = 128_000,
        *,
        output_reserve: int = 8192,
        safety_margin: int = 1024,
        max_input_ratio: float = 0.85,
        old_tool_result_chars: int = 4000,
        summarizer: Summarizer | None = None,
        summary_target_tokens: int = 1200,
    ):
        if max_tokens <= output_reserve + safety_margin:
            raise ValueError("context window must exceed output reserve and safety margin")
        if not 0 < max_input_ratio <= 1:
            raise ValueError("max input ratio must be greater than 0 and at most 1")
        if int(max_tokens * max_input_ratio) < 1:
            raise ValueError("max input ratio leaves no input budget")
        self.max_tokens = max_tokens
        self.output_reserve = output_reserve
        self.safety_margin = safety_margin
        self.max_input_ratio = max_input_ratio
        self.old_tool_result_chars = old_tool_result_chars
        self.summarizer = summarizer
        self.summary_target_tokens = max(128, summary_target_tokens)

    @property
    def input_budget(self) -> int:
        reserved_budget = self.max_tokens - self.output_reserve - self.safety_margin
        ratio_budget = int(self.max_tokens * self.max_input_ratio)
        return min(reserved_budget, ratio_budget)

    def prepare(
        self,
        system_prompt: str,
        active_messages: list[Message],
        tool_schemas: list[dict[str, Any]],
    ) -> PreparedContext:
        active = copy.deepcopy(active_messages)
        system = {"role": "system", "content": system_prompt}
        schema_tokens = estimate_tokens(tool_schemas)
        fixed_tokens = estimate_tokens(system) + schema_tokens
        if fixed_tokens >= self.input_budget:
            raise ContextLimitError(
                "System Prompt and Tool Schemas alone exceed the input budget"
            )

        before = fixed_tokens + estimate_tokens(active)
        if before <= self.input_budget:
            return PreparedContext(
                [system, *active],
                active,
                CompactionReport(before, before),
            )

        protected_tool_indexes = _latest_tool_batch_indexes(active)
        snipped = 0
        for index, message in enumerate(active):
            if message.get("role") != "tool" or index in protected_tool_indexes:
                continue
            content = message.get("content")
            if isinstance(content, str) and len(content) > self.old_tool_result_chars:
                omitted = len(content) - self.old_tool_result_chars
                message["content"] = (
                    content[: self.old_tool_result_chars]
                    + f"\n[older Tool Result snipped: {omitted} characters]"
                )
                snipped += 1

        collapsed = 0
        summarized = 0
        blocks = _message_blocks(active)

        # Preserve the newest protocol block verbatim. Older material can be summarized,
        # while the durable Transcript remains untouched by this operation.
        if self.summarizer is not None and len(blocks) > 1:
            current = fixed_tokens + estimate_tokens(_flatten(blocks))
            if current > self.input_budget:
                old_blocks = blocks[:-1]
                try:
                    summary = self.summarizer.summarize(
                        _flatten(old_blocks), self.summary_target_tokens
                    )
                except Exception:  # noqa: BLE001 - collapse is the deliberate fallback
                    summary = ""
                if summary:
                    maximum_chars = self.summary_target_tokens * 4
                    if len(summary) > maximum_chars:
                        summary = summary[:maximum_chars] + "\n[summary truncated]"
                    summarized = sum(len(block) for block in old_blocks)
                    blocks = [
                        [
                            {
                                "role": "system",
                                "content": (
                                    f"[Summary of {summarized} earlier messages]\n{summary}"
                                ),
                            }
                        ],
                        blocks[-1],
                    ]

        def collapsed_tokens() -> int:
            note_tokens = estimate_tokens(_compaction_note(collapsed)) if collapsed else 0
            return fixed_tokens + note_tokens + estimate_tokens(_flatten(blocks))

        while collapsed_tokens() > self.input_budget and len(blocks) > 1:
            removed = blocks.pop(0)
            if summarized and removed[0].get("role") == "system":
                collapsed += summarized
                summarized = 0
            else:
                collapsed += len(removed)

        if collapsed:
            note = _compaction_note(collapsed)
            active = [note, *_flatten(blocks)]
        else:
            active = _flatten(blocks)

        active = self._shrink_plain_content(active, fixed_tokens)
        after = fixed_tokens + estimate_tokens(active)
        if after > self.input_budget:
            raise ContextLimitError(
                f"request needs about {after} input tokens but budget is {self.input_budget}; "
                "the newest protocol block or Tool arguments are too large"
            )
        return PreparedContext(
            [system, *active],
            active,
            CompactionReport(before, after, snipped, collapsed, summarized),
        )

    def _shrink_plain_content(self, active: list[Message], fixed_tokens: int) -> list[Message]:
        """Last-resort deterministic shrink without breaking Tool Call structure."""
        current = fixed_tokens + estimate_tokens(active)
        if current <= self.input_budget:
            return active
        for message in active:
            if current <= self.input_budget:
                break
            content = message.get("content")
            if not isinstance(content, str) or len(content) <= 1000:
                continue
            remove_chars = min(len(content) - 1000, (current - self.input_budget) * 4)
            if remove_chars <= 0:
                continue
            message["content"] = (
                content[: len(content) - remove_chars]
                + f"\n[hard-truncated {remove_chars} characters]"
            )
            current = fixed_tokens + estimate_tokens(active)
        return active


def estimate_tokens(value: Any) -> int:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, (len(text) + 3) // 4)


def _latest_tool_batch_indexes(messages: list[Message]) -> set[int]:
    indexes: set[int] = set()
    for index in range(len(messages) - 1, -1, -1):
        role = messages[index].get("role")
        if role == "tool":
            indexes.add(index)
            continue
        if indexes and role == "assistant" and messages[index].get("tool_calls"):
            indexes.add(index)
        break
    return indexes


def _message_blocks(messages: list[Message]) -> list[list[Message]]:
    """Keep assistant Tool Calls and all following observations atomic."""
    blocks: list[list[Message]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        block = [message]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            while index < len(messages) and messages[index].get("role") == "tool":
                block.append(messages[index])
                index += 1
        blocks.append(block)
    return blocks


def _flatten(blocks: list[list[Message]]) -> list[Message]:
    return [message for block in blocks for message in block]


def _compaction_note(collapsed: int) -> Message:
    return {
        "role": "system",
        "content": (
            f"[Active Context compacted: {collapsed} earlier messages are available "
            "in the durable Transcript but omitted from this model request.]"
        ),
    }
