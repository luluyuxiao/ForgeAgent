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

