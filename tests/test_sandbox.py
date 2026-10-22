from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeagent.sandbox import DockerExecutor


def test_docker_executor_builds_hardened_fail_closed_command(tmp_path):
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    executor = DockerExecutor("sandbox:test", runner=runner)
    result = executor.execute("id", tmp_path, 10)
    command = captured["command"]

    assert result.stdout == "ok"
    assert "--read-only" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert command[-3:] == ["/bin/sh", "-lc", "id"]
    assert result.workspace_mode == "isolated"


def test_isolated_workspace_discards_container_deletions(tmp_path):
    protected = tmp_path / "protected.txt"
    protected.write_text("keep me", encoding="utf-8")

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / "protected.txt").unlink()
        (mounted / "generated.txt").write_text("temporary", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = DockerExecutor("sandbox:test", runner=runner).execute("mutate", tmp_path, 10)

    assert protected.read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "generated.txt").exists()
    assert result.workspace_changes == ("A generated.txt", "D protected.txt")


def test_direct_workspace_mode_explicitly_allows_container_deletions(tmp_path):
    target = tmp_path / "delete-me.txt"
    target.write_text("temporary", encoding="utf-8")

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / "delete-me.txt").unlink()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = DockerExecutor(
        "sandbox:test", workspace_mode="direct", runner=runner
    ).execute("mutate", tmp_path, 10)

    assert not target.exists()
    assert result.workspace_mode == "direct"
    assert result.workspace_changes == ("D delete-me.txt",)


def test_isolated_git_snapshot_includes_dirty_files_but_excludes_ignored_secrets(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    tracked.write_text("dirty working tree", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("visible", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hidden", encoding="utf-8")

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        assert (mounted / "tracked.txt").read_text(encoding="utf-8") == "dirty working tree"
        assert (mounted / "untracked.txt").read_text(encoding="utf-8") == "visible"
        assert not (mounted / ".env").exists()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    DockerExecutor("sandbox:test", runner=runner).execute("inspect", tmp_path, 10)


def test_docker_executor_rejects_unknown_workspace_mode():
    with pytest.raises(ValueError, match="workspace mode"):
        DockerExecutor(workspace_mode="unknown")
