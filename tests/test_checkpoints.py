from __future__ import annotations

from forgeagent.checkpoints import CheckpointManager
from forgeagent.contracts import ToolCall
from forgeagent.registry import ToolRegistry
from forgeagent.tools import build_builtin_tools


def test_undo_restores_modified_file(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    manager = CheckpointManager(tmp_path)
    call = ToolCall("c1", "write_file", {"path": "a.txt", "content": "after"})

    manager.capture(call, registry.require(call.name))
    registry.execute(call)

    assert target.read_text(encoding="utf-8") == "after"
    assert "restored a.txt" in manager.undo()
    assert target.read_text(encoding="utf-8") == "before"


def test_undo_removes_new_file_and_roundtrips_snapshot(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    manager = CheckpointManager(tmp_path)
    call = ToolCall("c1", "write_file", {"path": "new.txt", "content": "x"})
    manager.capture(call, registry.require(call.name))
    exported = manager.export()
    registry.execute(call)

    restored = CheckpointManager(tmp_path)
    restored.restore(exported)
    restored.undo()

    assert not (tmp_path / "new.txt").exists()
