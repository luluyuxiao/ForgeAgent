from __future__ import annotations

import json

import pytest

from forgeagent.contracts import Message, ToolCall
from forgeagent.tools import (
    Access,
    ReadTranscriptRangeTool,
    ResourceClaim,
    SearchTranscriptTool,
    ToolEffect,
)


def _live_search_call() -> Message:
    call = ToolCall("search-1", "search_transcript", {"query": "deployment port"})
    return {"role": "assistant", "content": None, "tool_calls": [call.as_openai_dict()]}


def test_search_transcript_finds_earlier_detail_and_excludes_live_turn():
    messages: list[Message] = [
        {"role": "user", "content": "Use deployment port 8137 for the preview server."},
        {"role": "assistant", "content": "I will remember port 8137."},
        {"role": "user", "content": "What was the deployment port?"},
        _live_search_call(),
    ]
    tool = SearchTranscriptTool(lambda: messages)

    result = json.loads(tool.execute("deployment port"))

    assert result["history_message_count"] == 2
    assert result["results"][0]["index"] == 0
    assert "8137" in result["results"][0]["snippet"]
    assert all(item["index"] < 2 for item in result["results"])


def test_search_transcript_filters_roles_and_validates_arguments():
    messages: list[Message] = [
        {"role": "user", "content": "The codename is cobalt."},
        {"role": "assistant", "content": "Cobalt recorded."},
    ]
    tool = SearchTranscriptTool(lambda: messages)

    result = json.loads(tool.execute("cobalt", roles=["assistant"], limit=1))

    assert [item["role"] for item in result["results"]] == ["assistant"]
    with pytest.raises(ValueError, match="unknown roles"):
        tool.execute("cobalt", roles=["developer"])
    with pytest.raises(ValueError, match="between 1 and 20"):
        tool.execute("cobalt", limit=21)


def test_read_transcript_range_preserves_indexes_and_bounds_large_messages():
    messages: list[Message] = [
        {"role": "user", "content": "a" * 10_000},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "recover that text"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                ToolCall("read-1", "read_transcript_range", {"start": 0}).as_openai_dict()
            ],
        },
    ]
    tool = ReadTranscriptRangeTool(lambda: messages)

    result_text = tool.execute(0, count=8)
    result = json.loads(result_text)

    assert result["history_message_count"] == 2
    assert [message["index"] for message in result["messages"]] == [0, 1]
    assert result["messages"][0]["truncated"] is True
    assert len(result_text) < 10_000
    assert json.loads(tool.execute(20))["messages"] == []


def test_transcript_tools_declare_read_only_authority_and_resource():
    search = SearchTranscriptTool(list)
    read = ReadTranscriptRangeTool(list)

    for tool in (search, read):
        assert tool.effect is ToolEffect.READ
        assert tool.capabilities == frozenset({"conversation_read"})
        assert tool.parallel_safe
        assert tool.resources({}) == (
            ResourceClaim("conversation:transcript", Access.READ),
        )
        assert tool.schema()["function"]["parameters"]["additionalProperties"] is False
