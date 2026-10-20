from __future__ import annotations

import pytest

from forgeagent.contracts import ModelReply, Usage
from forgeagent.providers import FallbackProvider, PartialResponseError
from tests.helpers import ScriptedProvider


class FailingProvider(ScriptedProvider):
    def __init__(self, model="broken", *, emits=False):
        super().__init__([], model=model)
        self.emits = emits

    def complete(self, messages, tools, *, on_text=None):
        if self.emits and on_text:
            on_text("partial")
        raise ConnectionError("offline")


def test_fallback_switches_before_output_and_becomes_sticky():
    backup = ScriptedProvider(
        [ModelReply(content="ok", usage=Usage(2, 1)), ModelReply(content="again")],
        model="backup",
    )
    provider = FallbackProvider([FailingProvider(), backup])

    assert provider.complete([], []).content == "ok"
    assert provider.model == "backup"
    assert provider.complete([], []).content == "again"
    assert provider.total_usage.total_tokens == 3


def test_fallback_refuses_to_replay_after_visible_output():
    provider = FallbackProvider(
        [FailingProvider(emits=True), ScriptedProvider([ModelReply(content="duplicate")])]
    )

    with pytest.raises(PartialResponseError):
        provider.complete([], [], on_text=lambda value: None)
