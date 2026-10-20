from __future__ import annotations

from forgeagent.context import ContextWindow
from forgeagent.contracts import ModelReply, ToolCall, Usage
from forgeagent.policies import PermissionPolicy, PlanningPolicy, PolicyPipeline, RunMode
from forgeagent.registry import ToolRegistry
from forgeagent.runtime import AgentRuntime
from forgeagent.tools import build_builtin_tools

from tests.helpers import ScriptedProvider


def _runtime(tmp_path, replies, *, allow_all=True, max_rounds=5):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    provider = ScriptedProvider(replies)
    return AgentRuntime(
        provider=provider,
        registry=registry,
        policies=PolicyPipeline(
            [PlanningPolicy(), PermissionPolicy(allow_all=allow_all)]
        ),
        context=ContextWindow(4000, output_reserve=200, safety_margin=100),
        max_rounds=max_rounds,
        workspace=tmp_path,
    )


def test_plain_response_finishes_in_one_round(tmp_path):
    runtime = _runtime(
        tmp_path,
        [ModelReply(content="done", usage=Usage(10, 2))],
    )

    assert runtime.run("hello") == "done"
    assert [message["role"] for message in runtime.transcript] == ["user", "assistant"]


def test_tool_result_enters_next_model_request(tmp_path):
    runtime = _runtime(
        tmp_path,
        [
            ModelReply(tool_calls=[ToolCall("c1", "now", {})]),
            ModelReply(content="observed"),
        ],
    )

    assert runtime.run("what time is it") == "observed"
    second_request = runtime.provider.requests[1][0]
    tool_messages = [message for message in second_request if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c1"


def test_plan_mode_denies_write_and_returns_reason_to_model(tmp_path):
    target = tmp_path / "blocked.txt"
    runtime = _runtime(
        tmp_path,
        [
            ModelReply(
                tool_calls=[
                    ToolCall("c1", "write_file", {"path": "blocked.txt", "content": "x"})
                ]
            ),
            ModelReply(content="Here is the plan."),
        ],
    )
    runtime.set_mode(RunMode.PLANNING)

    assert runtime.run("change the file") == "Here is the plan."
    assert not target.exists()
    observations = [message for message in runtime.transcript if message.get("role") == "tool"]
    assert "Plan Mode rejected" in observations[0]["content"]


def test_unknown_tool_still_gets_one_observation(tmp_path):
    runtime = _runtime(
        tmp_path,
        [
            ModelReply(tool_calls=[ToolCall("c1", "missing", {})]),
            ModelReply(content="recovered"),
        ],
    )

    assert runtime.run("go") == "recovered"
    result = next(message for message in runtime.transcript if message.get("role") == "tool")
    assert result["tool_call_id"] == "c1"
    assert "unknown Tool" in result["content"]


def test_round_limit_is_enforced(tmp_path):
    runtime = _runtime(
        tmp_path,
        [
            ModelReply(tool_calls=[ToolCall("c1", "now", {})]),
            ModelReply(tool_calls=[ToolCall("c2", "now", {})]),
        ],
        max_rounds=2,
    )

    result = runtime.run("loop")

    assert result == "Stopped after reaching the 2-round limit."
    assert runtime.transcript[-1] == {"role": "assistant", "content": result}

