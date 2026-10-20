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
from forgeagent.registry import ToolRegistry
from forgeagent.tools import build_builtin_tools


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

