from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from forgeagent.context import ContextWindow
from forgeagent.contracts import ModelReply, ToolCall
from forgeagent.hooks import ToolHooks
from forgeagent.policies import PermissionPolicy, PolicyPipeline
from forgeagent.runtime import AgentRuntime
from forgeagent.sandbox import CommandResult
from forgeagent.tools import ToolRegistry, build_builtin_tools
from tests.helpers import ScriptedProvider

EXAMPLES = Path(__file__).parents[1] / "examples"


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


def _run_example_hook(script: str, payload: dict, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXAMPLES / script)],
        cwd=cwd,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_example_guard_blocks_credentials_and_risky_shell(tmp_path):
    credential = _run_example_hook(
        "hook_guard.py",
        {"tool_name": "replace_text", "tool_input": {"path": "config/.env.local"}},
        cwd=tmp_path,
    )
    shell = _run_example_hook(
        "hook_guard.py",
        {"tool_name": "shell", "tool_input": {"command": "git reset --hard HEAD"}},
        cwd=tmp_path,
    )
    safe = _run_example_hook(
        "hook_guard.py",
        {"tool_name": "write_file", "tool_input": {"path": ".env.example"}},
        cwd=tmp_path,
    )

    assert credential.returncode == 2
    assert "credential file is protected" in credential.stderr
    assert shell.returncode == 2
    assert "potentially destructive" in shell.stderr
    assert safe.returncode == 0


def test_example_post_hook_writes_content_free_audit_record(tmp_path):
    completed = _run_example_hook(
        "hook_audit.py",
        {
            "tool_name": "write_file",
            "tool_input": {"path": "secret.txt", "content": "do not log me"},
            "tool_response": "Wrote secret.txt",
        },
        cwd=tmp_path,
    )

    assert completed.returncode == 0
    record = json.loads((tmp_path / ".forgeagent/hook-audit.jsonl").read_text())
    assert record["tool_name"] == "write_file"
    assert record["input_keys"] == ["content", "path"]
    assert record["result_chars"] == len("Wrote secret.txt")
    assert "do not log me" not in json.dumps(record)
