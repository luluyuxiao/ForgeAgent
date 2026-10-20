"""Optional provider-backed Active Context summarization."""

from __future__ import annotations

import json
import threading

from .contracts import ChatProvider, Message


class ProviderSummarizer:
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
