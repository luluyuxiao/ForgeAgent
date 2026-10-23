from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from forgeagent.checkpoints import CheckpointManager
from forgeagent.contracts import ToolCall
from forgeagent.policies import PermissionPolicy, PolicyContext
from forgeagent.sandbox import DockerExecutor
from forgeagent.tools import (
    ApplySandboxChangesTool,
    ResetSandboxSessionTool,
    ReviewSandboxChangesTool,
    ToolEffect,
    Workspace,
)


def _isolated_changes(tmp_path):
    (tmp_path / "modify.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("remove\n", encoding="utf-8")

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / "modify.txt").write_text("after\n", encoding="utf-8")
        (mounted / "delete.txt").unlink()
        (mounted / "new.txt").write_text("created\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    executor = DockerExecutor("sandbox:test", runner=runner)
    result = executor.execute("mutate", tmp_path, 10)
    return executor, result


def test_review_and_apply_isolated_changes_with_checkpoint_undo(tmp_path):
    executor, result = _isolated_changes(tmp_path)
    store = executor.change_store(tmp_path)
    review = ReviewSandboxChangesTool(store)
    apply = ApplySandboxChangesTool(
        Workspace(tmp_path),
        store,
        on_applied=lambda: executor.discard_session(tmp_path),
    )

    reviewed = json.loads(review.execute(result.change_set_id))
    assert reviewed["summary"] == ["D delete.txt", "M modify.txt", "A new.txt"]
    assert reviewed["paths"] == ["delete.txt", "modify.txt", "new.txt"]
    assert "-before" in reviewed["patch"]
    assert apply.effect is ToolEffect.WRITE
    assert apply.parameters["required"] == ["change_set_id"]

    call = ToolCall(
        "apply-1",
        "apply_sandbox_changes",
        {"change_set_id": result.change_set_id},
    )
    manager = CheckpointManager(tmp_path)
    manager.begin(call, apply)
    applied = apply.execute(result.change_set_id)
    assert manager.complete(call.id, succeeded=not applied.startswith("Error")) == "committed"

    assert (tmp_path / "modify.txt").read_text(encoding="utf-8") == "after\n"
    assert not (tmp_path / "delete.txt").exists()
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert not executor.discard_session(tmp_path)
    assert "Undid apply_sandbox_changes" in manager.undo()
    assert (tmp_path / "modify.txt").read_text(encoding="utf-8") == "before\n"
    assert (tmp_path / "delete.txt").read_text(encoding="utf-8") == "remove\n"
    assert not (tmp_path / "new.txt").exists()


def test_apply_rejects_real_workspace_change_after_snapshot(tmp_path):
    executor, result = _isolated_changes(tmp_path)
    tool = ApplySandboxChangesTool(Workspace(tmp_path), executor.change_store(tmp_path))
    (tmp_path / "modify.txt").write_text("user edit\n", encoding="utf-8")

    applied = tool.execute(result.change_set_id)

    assert applied.startswith("Error: sandbox change set conflict")
    assert "modify.txt" in applied
    assert (tmp_path / "modify.txt").read_text(encoding="utf-8") == "user edit\n"
    assert (tmp_path / "delete.txt").exists()
    assert not (tmp_path / "new.txt").exists()


def test_apply_change_set_requires_write_permission(tmp_path):
    executor, result = _isolated_changes(tmp_path)
    tool = ApplySandboxChangesTool(Workspace(tmp_path), executor.change_store(tmp_path))
    call = ToolCall(
        "apply-1",
        tool.name,
        {"change_set_id": result.change_set_id},
    )

    denied = PermissionPolicy().evaluate(call, tool, PolicyContext())
    approved = PermissionPolicy(allow_all=True).evaluate(call, tool, PolicyContext())

    assert not denied.allowed and denied.code == "non_interactive"
    assert approved.allowed


def test_unknown_change_set_is_a_bounded_tool_error(tmp_path):
    executor = DockerExecutor("sandbox:test", runner=lambda *_args, **_kwargs: None)
    store = executor.change_store(tmp_path)
    review = ReviewSandboxChangesTool(store)
    apply = ApplySandboxChangesTool(Workspace(tmp_path), store)

    assert review.execute("missing").startswith("Error: unknown or expired")
    assert apply.checkpoint_paths({"change_set_id": "missing"}) == ()
    assert apply.execute("missing").startswith("Error: cannot validate")


def test_reset_sandbox_session_discards_pending_state(tmp_path):
    executor, result = _isolated_changes(tmp_path)
    tool = ResetSandboxSessionTool(lambda: executor.discard_session(tmp_path))

    reset = tool.execute()

    assert reset.startswith("Discarded")
    assert ReviewSandboxChangesTool(executor.change_store(tmp_path)).execute(
        result.change_set_id
    ).startswith("Error: unknown or expired")
    assert tool.execute() == "No isolated sandbox session is active."
