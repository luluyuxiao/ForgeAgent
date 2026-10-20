from __future__ import annotations

import json
import subprocess
from typing import Any, ClassVar

import pytest

from forgeagent.contracts import ToolCall
from forgeagent.registry import ToolRegistry
from forgeagent.sandbox import CommandResult
from forgeagent.tools import Tool, ToolEffect, build_builtin_tools


class EchoTool(Tool):
    name = "echo"
    description = "echo text"
    effect = ToolEffect.PURE
    capabilities = frozenset()
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, text: str) -> str:
        return text


def test_registry_generates_schema_and_executes():
    registry = ToolRegistry([EchoTool()])

    assert registry.schemas()[0]["function"]["name"] == "echo"
    assert registry.execute(ToolCall("1", "echo", {"text": "hello"})) == "hello"


def test_registry_rejects_duplicates_and_bad_arguments():
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([EchoTool(), EchoTool()])

    registry = ToolRegistry([EchoTool()])
    result = registry.execute(ToolCall("1", "echo", {"wrong": "value"}))
    assert result.startswith("Error: invalid arguments")


def test_file_tools_stay_inside_workspace(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    written = registry.execute(
        ToolCall("1", "write_file", {"path": "src/a.txt", "content": "hello"})
    )
    read = registry.execute(ToolCall("2", "read_file", {"path": "src/a.txt"}))
    escaped = registry.execute(ToolCall("3", "read_file", {"path": "../outside.txt"}))

    assert written.startswith("Wrote")
    assert "hello" in read
    assert "escapes workspace" in escaped


def test_unique_replace_refuses_ambiguous_text(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    registry = ToolRegistry(build_builtin_tools(tmp_path))

    result = registry.execute(
        ToolCall("1", "replace_text", {"path": "a.txt", "old": "same", "new": "new"})
    )

    assert "found 2 occurrences" in result
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_git_tools_apply_patch_and_report_status(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    target = tmp_path / "hello.txt"
    target.write_text("hello\n", encoding="utf-8")
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    patch = """\
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+goodbye
"""

    applied = registry.execute(ToolCall("1", "apply_patch", {"patch": patch}))
    status = registry.execute(ToolCall("2", "git_status", {}))

    assert applied == "Applied patch to: hello.txt"
    assert target.read_text(encoding="utf-8") == "goodbye\n"
    assert "hello.txt" in status


def test_run_checks_uses_only_project_configuration(tmp_path):
    class FakeExecutor:
        name = "fake"
        network = "none"

        def __init__(self):
            self.commands = []

        def execute(self, command, cwd, timeout):
            self.commands.append((command, cwd, timeout))
            return CommandResult(0, "ok", "")

    (tmp_path / ".forgeagent.json").write_text(
        json.dumps({"checks": {"lint": "ruff check .", "test": ["pytest -q"]}}),
        encoding="utf-8",
    )
    executor = FakeExecutor()
    registry = ToolRegistry(build_builtin_tools(tmp_path, executor=executor))

    result = json.loads(
        registry.execute(ToolCall("1", "run_checks", {"kind": "all", "timeout": 10}))
    )

    assert result["passed"]
    assert [item[0] for item in executor.commands] == ["ruff check .", "pytest -q"]
