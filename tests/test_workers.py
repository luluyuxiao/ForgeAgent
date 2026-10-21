from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from forgeagent.workers import (
    ChildRunResult,
    SubAgentManager,
    SubAgentTool,
    subagent_tool_allowlist,
)


def test_foreground_child_returns_result_and_failure_is_contained():
    manager = SubAgentManager(lambda task: ChildRunResult(task.upper(), 12, 3))
    try:
        done = manager.submit("review", background=False)
        assert done.status == "completed"
        assert done.result == "REVIEW"
        assert done.profile == "research"
        assert done.prompt_tokens == 12
        assert done.completion_tokens == 3
    finally:
        manager.close()


def test_profile_is_passed_to_a_three_argument_child_runner(tmp_path):
    observed = []

    def run(task, workspace, profile):
        observed.append((task, workspace, profile))
        return profile

    manager = SubAgentManager(run, workspace=tmp_path)
    try:
        record = manager.submit("edit", background=False, profile="implement")

        assert record.status == "completed"
        assert record.profile == "implement"
        assert record.result == "implement"
        assert observed == [("edit", tmp_path.resolve(), "implement")]
    finally:
        manager.close()


def test_subagent_profiles_only_narrow_parent_tools():
    research = subagent_tool_allowlist("research")
    implement = subagent_tool_allowlist("implement")
    parent = frozenset({"read_file", "write_file", "shell"})

    assert research is not None
    assert "read_file" in research
    assert "write_file" not in research
    assert implement is not None
    assert "write_file" in implement
    assert "run_checks" in implement
    assert "shell" not in implement
    assert subagent_tool_allowlist("implement", parent) == frozenset(
        {"read_file", "write_file"}
    )
    assert subagent_tool_allowlist("full", parent) == parent


def test_subagent_tool_validates_and_reports_profile():
    manager = SubAgentManager(lambda task: task)
    tool = SubAgentTool(manager)
    try:
        assert tool.execute("task", profile="invalid").startswith("Error: profile")
        result = json.loads(tool.execute("task", profile="research"))
        assert result["profile"] == "research"
    finally:
        manager.close()


def test_background_worker_exposes_state():
    def run(task):
        time.sleep(0.03)
        return f"done:{task}"

    manager = SubAgentManager(run, max_workers=1)
    try:
        queued = manager.submit("x", background=True)
        deadline = time.time() + 1
        while manager.get(queued.task_id).status not in {"completed", "failed"}:
            assert time.time() < deadline
            time.sleep(0.01)
        assert manager.get(queued.task_id).result == "done:x"
    finally:
        manager.close()


def test_worktree_isolation_creates_retained_branch(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repository, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)

    manager = SubAgentManager(
        lambda task, workspace: str(workspace),
        workspace=repository,
        worktree_root=tmp_path / "worktrees",
    )
    try:
        record = manager.submit("review", background=False, isolation="worktree")
        assert record.status == "completed"
        assert record.branch.startswith("forgeagent/agent-")
        assert record.workspace != str(repository)
        assert (tmp_path / "worktrees") in Path(record.workspace).parents
    finally:
        manager.close()
