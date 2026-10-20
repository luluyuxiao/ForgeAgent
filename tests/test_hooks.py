from __future__ import annotations

import json

from forgeagent.context import ContextWindow
from forgeagent.contracts import ModelReply, ToolCall
from forgeagent.hooks import ToolHooks
from forgeagent.policies import PermissionPolicy, PolicyPipeline
from forgeagent.runtime import AgentRuntime
from forgeagent.sandbox import CommandResult
from forgeagent.tools import ToolRegistry, build_builtin_tools
from tests.helpers import ScriptedProvider


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, command, cwd, timeout, *, stdin=None):
        self.calls.append((command, cwd, timeout, json.loads(stdin)))
        if command == "deny":
            return CommandResult(2, "", "protected path")
        return CommandResult(0, "ok", "")


def test_pre_hook_can_block_and_post_hook_observes(tmp_path):
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "PreToolUse": [{"matcher": "write_*", "command": "deny"}],
                "PostToolUse": [{"matcher": "*", "command": "observe"}],
            }
        ),
        encoding="utf-8",
    )
    executor = FakeExecutor()
    hooks = ToolHooks.load(config, executor=executor, workspace=tmp_path)

    assert hooks.run_pre("write_file", {"path": "x"}) == "Blocked by hook: protected path"
    assert hooks.run_pre("read_file", {"path": "x"}) is None
    hooks.run_post("read_file", {"path": "x"}, "content")

    assert executor.calls[-1][3]["tool_response"] == "content"


def test_blocked_pre_hook_prevents_tool_execution(tmp_path):
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps({"PreToolUse": [{"matcher": "write_file", "command": "deny"}]}),
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall("c1", "write_file", {"path": "blocked.txt", "content": "x"})
                ]
            ),
            ModelReply(content="blocked safely"),
        ]
    )
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(build_builtin_tools(tmp_path)),
        policies=PolicyPipeline([PermissionPolicy(allow_all=True)]),
        context=ContextWindow(4_000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
        hooks=ToolHooks.load(config, executor=FakeExecutor(), workspace=tmp_path),
    )

    assert runtime.run("write it") == "blocked safely"
    assert not (tmp_path / "blocked.txt").exists()
    observation = next(item for item in runtime.transcript if item.get("role") == "tool")
    assert observation["content"] == "Blocked by hook: protected path"
