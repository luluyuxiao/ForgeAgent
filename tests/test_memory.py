from __future__ import annotations

import json

from forgeagent.memory import MemoryUpdateTool, TaskMemory


def test_structured_memory_tracks_goal_plan_decisions_and_files():
    memory = TaskMemory()
    tool = MemoryUpdateTool(memory)

    tool.execute("set_goal", content="Fix the parser")
    tool.execute("add_constraint", content="Do not change the public API")
    step = json.loads(tool.execute("add_plan_step", content="Reproduce the bug"))
    tool.execute("update_plan_step", item_id=step["id"], status="completed")
    tool.execute("add_decision", content="Keep JSON compatibility", reason="users rely on it")
    memory.record_file("src/parser.py")

    rendered = memory.render()
    assert "Fix the parser" in rendered
    assert "Do not change the public API" in rendered
    assert "[completed]" in rendered
    assert "src/parser.py" in rendered
    assert TaskMemory.from_dict(memory.to_dict()).to_dict() == memory.to_dict()


def test_memory_restore_keeps_tool_reference_current():
    memory = TaskMemory()
    tool = MemoryUpdateTool(memory)

    memory.restore({"goal": "restored"})
    tool.execute("add_plan_step", content="Run tests")

    assert memory.goal == "restored"
    assert memory.plan[0]["content"] == "Run tests"
