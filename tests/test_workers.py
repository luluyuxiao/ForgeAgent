from __future__ import annotations

import subprocess
import time
from pathlib import Path

from forgeagent.workers import ChildRunResult, SubAgentManager


def test_foreground_child_returns_result_and_failure_is_contained():
    manager = SubAgentManager(lambda task: ChildRunResult(task.upper(), 12, 3))
    try:
        done = manager.submit("review", background=False)
        assert done.status == "completed"
        assert done.result == "REVIEW"
        assert done.prompt_tokens == 12
        assert done.completion_tokens == 3
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
