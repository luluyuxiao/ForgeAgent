from __future__ import annotations

import copy

from forgeagent.context import ContextWindow


def _assistant_call(call_id: str):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }


def test_input_budget_uses_stricter_hybrid_limit():
    ratio_limited = ContextWindow(
        10_000,
        output_reserve=1_000,
        safety_margin=500,
        max_input_ratio=0.8,
    )
    reserve_limited = ContextWindow(
        10_000,
        output_reserve=3_000,
        safety_margin=1_000,
        max_input_ratio=0.8,
    )

    assert ratio_limited.input_budget == 8_000
    assert reserve_limited.input_budget == 6_000


def test_old_tool_result_is_snipped_without_mutating_source():
    messages = [
        _assistant_call("old"),
        {"role": "tool", "tool_call_id": "old", "content": "x" * 1200},
        {"role": "user", "content": "continue"},
    ]
    original = copy.deepcopy(messages)
    window = ContextWindow(
        350,
        output_reserve=30,
        safety_margin=20,
        old_tool_result_chars=80,
    )

    prepared = window.prepare("system", messages, [])

    assert prepared.report.snipped_tool_results == 1
    assert "snipped" in prepared.active_messages[1]["content"]
    assert messages == original


def test_collapse_keeps_tool_call_and_result_in_same_block():
    messages = []
    for index in range(8):
        messages.extend(
            [
                _assistant_call(str(index)),
                {"role": "tool", "tool_call_id": str(index), "content": "value " * 100},
                {"role": "user", "content": f"next {index}"},
            ]
        )
    window = ContextWindow(
        500,
        output_reserve=40,
        safety_margin=20,
        old_tool_result_chars=60,
    )

    prepared = window.prepare("system", messages, [])
    active = prepared.active_messages

    assert prepared.report.collapsed_messages > 0
    assert active[0]["role"] == "system"
    for index, message in enumerate(active):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected = {call["id"] for call in message["tool_calls"]}
            observed = set()
            cursor = index + 1
            while cursor < len(active) and active[cursor].get("role") == "tool":
                observed.add(active[cursor]["tool_call_id"])
                cursor += 1
            assert observed == expected


def test_llm_summary_replaces_old_blocks_but_not_source():
    class FakeSummarizer:
        def __init__(self):
            self.seen = []

        def summarize(self, messages, target_tokens):
            self.seen = messages
            return "important old decision"

    messages = [{"role": "user", "content": f"old {index} " * 200} for index in range(6)]
    original = copy.deepcopy(messages)
    summarizer = FakeSummarizer()
    window = ContextWindow(
        500,
        output_reserve=40,
        safety_margin=20,
        summarizer=summarizer,
        summary_target_tokens=128,
    )

    prepared = window.prepare("system", messages, [])

    assert prepared.report.summarized_messages > 0
    assert any("important old decision" in str(item.get("content")) for item in prepared.active_messages)
    assert summarizer.seen
    assert messages == original
