from __future__ import annotations

from forgeagent.contracts import ToolCall
from forgeagent.policies import (
    CapabilityPolicy,
    PermissionPolicy,
    PlanningPolicy,
    PolicyContext,
    PolicyPipeline,
    RunMode,
)
from forgeagent.tools import ToolRegistry, build_builtin_tools


def _call(name, **arguments):
    return ToolCall("call", name, arguments)


def test_plan_mode_uses_effect_not_tool_name(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    policy = PlanningPolicy()
    context = PolicyContext(RunMode.PLANNING)

    read = policy.evaluate(_call("read_file", path="a"), registry.require("read_file"), context)
    write = policy.evaluate(
        _call("write_file", path="a", content="x"), registry.require("write_file"), context
    )

    assert read.allowed
    assert not write.allowed
    assert write.code == "planning_side_effect"


def test_capability_and_permission_are_independent(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    call = _call("shell", command="true")
    tool = registry.require("shell")
    pipeline = PolicyPipeline(
        [
            CapabilityPolicy({"process", "filesystem_read", "filesystem_write"}),
            PermissionPolicy(allow_all=True),
        ]
    )

    decision = pipeline.evaluate(call, tool, PolicyContext())

    assert not decision.allowed
    assert decision.policy == "capability"
    assert "network" in decision.reason


def test_noninteractive_permission_fails_closed(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    call = _call("write_file", path="a", content="x")

    decision = PermissionPolicy().evaluate(
        call, registry.require("write_file"), PolicyContext()
    )

    assert not decision.allowed
    assert decision.code == "non_interactive"


def test_always_permission_is_scoped_to_the_exact_shell_command(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    answers = iter(["always", "deny"])
    policy = PermissionPolicy(lambda _name, _arguments: next(answers))
    tool = registry.require("shell")

    first = policy.evaluate(_call("shell", command="pytest -q"), tool, PolicyContext())
    repeated = policy.evaluate(_call("shell", command="pytest -q"), tool, PolicyContext())
    different = policy.evaluate(_call("shell", command="git push"), tool, PolicyContext())

    assert first.allowed
    assert repeated.allowed and repeated.code == "always_cached"
    assert not different.allowed
