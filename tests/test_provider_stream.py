from __future__ import annotations

from types import SimpleNamespace

from forgeagent.providers import OpenAICompatibleProvider


def _chunk(*, text=None, calls=None, usage=None):
    delta = SimpleNamespace(content=text, tool_calls=calls or [])
    choices = [SimpleNamespace(delta=delta)] if text is not None or calls else []
    return SimpleNamespace(model="fake-model", choices=choices, usage=usage)


def _piece(index, *, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


def test_stream_reassembles_fragmented_tool_arguments():
    provider = OpenAICompatibleProvider(model="fake", api_key="test")
    stream = [
        _chunk(text="thinking "),
        _chunk(calls=[_piece(0, call_id="c1", name="read_", arguments='{"pa')]),
        _chunk(calls=[_piece(0, name="file", arguments='th":"a.txt"}')]),
        _chunk(
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
        ),
    ]
    tokens = []

    reply, emitted = provider._consume(stream, tokens.append)

    assert emitted
    assert tokens == ["thinking "]
    assert reply.content == "thinking "
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].arguments == {"path": "a.txt"}
    assert reply.usage.total_tokens == 25


def test_malformed_arguments_become_a_parse_error():
    provider = OpenAICompatibleProvider(model="fake", api_key="test")
    stream = [_chunk(calls=[_piece(0, call_id="c1", name="x", arguments="{")])]

    reply, _ = provider._consume(stream, None)

    assert reply.tool_calls[0].arguments == {}
    assert "invalid Tool Call arguments" in reply.tool_calls[0].parse_error

