from __future__ import annotations

import pytest

from forgeagent.checkpoints import CheckpointError, CheckpointManager
from forgeagent.contracts import ToolCall
from forgeagent.tools import ToolRegistry, build_builtin_tools


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


def test_committed_checkpoint_refuses_to_overwrite_a_later_edit(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    manager = CheckpointManager(tmp_path)
    call = ToolCall("c1", "write_file", {"path": "a.txt", "content": "agent"})

    manager.begin(call, registry.require(call.name))
    assert not registry.execute(call).startswith("Error")
    assert manager.complete(call.id, succeeded=True) == "committed"
    target.write_text("user edit", encoding="utf-8")

    result = manager.undo()

    assert "Undo blocked" in result
    assert "a.txt" in result
    assert target.read_text(encoding="utf-8") == "user edit"
    assert manager.depth == 1


def test_failed_call_is_rolled_back_without_adding_undo_history(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    manager = CheckpointManager(tmp_path)
    call = ToolCall(
        "c1",
        "replace_text",
        {"path": "a.txt", "old": "missing", "new": "after"},
    )

    manager.begin(call, registry.require(call.name))
    result = registry.execute(call)

    assert result.startswith("Error")
    assert manager.complete(call.id, succeeded=False) == "rolled_back"
    assert manager.depth == 0
    assert target.read_text(encoding="utf-8") == "before"


def test_checkpoint_rejects_a_file_over_the_byte_limit(tmp_path):
    target = tmp_path / "large.txt"
    target.write_bytes(b"1234")
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    manager = CheckpointManager(tmp_path, max_file_bytes=3)
    call = ToolCall("c1", "write_file", {"path": "large.txt", "content": "new"})

    with pytest.raises(CheckpointError, match="exceeds 3 bytes"):
        manager.begin(call, registry.require(call.name))


def test_committed_checkpoint_roundtrip_keeps_conflict_metadata(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    manager = CheckpointManager(tmp_path)
    call = ToolCall("c1", "write_file", {"path": "a.txt", "content": "after"})

    manager.begin(call, registry.require(call.name))
    registry.execute(call)
    manager.complete(call.id, succeeded=True)

    restored = CheckpointManager(tmp_path)
    restored.restore(manager.export())

    assert "restored a.txt" in restored.undo()
    assert target.read_text(encoding="utf-8") == "before"
